"""PRD outcome tests using synthetic XML, never the user's real database."""
from decimal import Decimal
from hashlib import sha256
import io, zipfile, csv, errno
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import app
from app.db import engine, Base, SessionLocal
from app.models import Invoice, FileObject, DurableTask, ImportEntry, UserCompanyScope, InvoiceCompanyRole
from app.services.jobs import run_pending_once, execute_task
from app.services import importer
from app.config import settings

CNPJ="12345678000199"
OTHER="98765432000111"

def key(number):
    body=f"352607{CNPJ}55001{number:09d}1{number:08d}"
    assert len(body)==43
    rest=sum(int(c)*(2+n%8) for n,c in enumerate(reversed(body)))%11
    return body+str(0 if rest<2 else 11-rest)

def xml(number,amount,*,issuer=CNPJ,recipient="",tp="1",fin="1",cfop="5102",authorized=True,issued="2026-07-31T23:30:00-03:00",items=None,env="1"):
    rows=items or [("A","Produto A",str(amount),cfop)]
    dets=''.join(f'<det nItem="{n}"><prod><cProd>{sku}</cProd><xProd>{name}</xProd><NCM>12345678</NCM><CFOP>{code}</CFOP><qCom>1</qCom><vUnCom>{value}</vUnCom><vProd>{value}</vProd></prod></det>' for n,(sku,name,value,code) in enumerate(rows,1))
    body=f'<NFe xmlns="http://www.portalfiscal.inf.br/nfe"><infNFe versao="4.00" Id="NFe{key(number)}"><ide><tpAmb>{env}</tpAmb><mod>55</mod><serie>1</serie><nNF>{number}</nNF><dhEmi>{issued}</dhEmi><tpNF>{tp}</tpNF><finNFe>{fin}</finNFe></ide><emit><CNPJ>{issuer}</CNPJ><xNome>SYNTHETIC COMPANY</xNome></emit><dest><CNPJ>{recipient}</CNPJ></dest>{dets}<total><ICMSTot><vNF>{amount}</vNF><vProd>{amount}</vProd><vDesc>0</vDesc></ICMSTot></total></infNFe></NFe>'
    if authorized: body=f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">{body}<protNFe><infProt><tpAmb>{env}</tpAmb><chNFe>{key(number)}</chNFe><cStat>100</cStat><nProt>123456789</nProt></infProt></protNFe></nfeProc>'
    return body.encode()

def cancel(number,accepted=True):
    receipt=f'<retEvento><infEvento><tpAmb>1</tpAmb><chNFe>{key(number)}</chNFe><tpEvento>110111</tpEvento><nSeqEvento>1</nSeqEvento><cStat>135</cStat><nProt>123456789</nProt></infEvento></retEvento>' if accepted else ''
    return f'<procEventoNFe xmlns="http://www.portalfiscal.inf.br/nfe"><evento><infEvento><tpAmb>1</tpAmb><CNPJ>{CNPJ}</CNPJ><chNFe>{key(number)}</chNFe><tpEvento>110111</tpEvento><nSeqEvento>1</nSeqEvento><dhEvento>2026-08-02T09:00:00-03:00</dhEvento></infEvento></evento>{receipt}</procEventoNFe>'.encode()

@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        assert c.post('/api/auth/bootstrap',json={'login':'admin','password':'SYNTHETIC-password-123'}).status_code==200
        assert c.post('/api/auth/login',json={'login':'admin','password':'SYNTHETIC-password-123'}).status_code==200
        assert c.post('/api/companies',json={'cnpj':CNPJ,'legal_name':'SYNTHETIC','confirmed':True}).status_code==200
        yield c

def upload(c,raw,name='sample.xml',run=True):
    r=c.post('/api/imports',files={'files':(name,raw,'application/octet-stream')})
    assert r.status_code==200,r.text
    if run:
        assert run_pending_once()
        return c.get(f"/api/imports/{r.json()['id']}").json()
    return r.json()

def summary(c): return c.get('/api/stats/summary').json()

def test_complete_business_chain_and_local_dates(client):
    c=client
    upload(c,xml(1,100,items=[('A','Alpha','60','5102'),('B','Beta','40','5102')]))
    upload(c,xml(2,200));upload(c,xml(3,500,issuer=OTHER,recipient=CNPJ))
    assert Decimal(summary(c)['sales_amount'])==300
    assert c.get('/api/invoices?sku=A&product_name=Beta').json()['total']==0
    match=c.get('/api/invoices?sku=A&product_name=Alpha').json()
    assert match['total']==1 and Decimal(match['invoice_amount'])==100 and Decimal(match['matched_items_amount'])==60
    upload(c,xml(4,50,authorized=False));assert Decimal(summary(c)['sales_amount'])==300
    assert upload(c,xml(4,50))['counters']['updated']==1
    assert Decimal(summary(c)['sales_amount'])==350
    upload(c,cancel(1,False));assert Decimal(summary(c)['sales_amount'])==350
    upload(c,cancel(1));assert Decimal(summary(c)['sales_amount'])==250
    upload(c,xml(5,40,tp='0',fin='4',cfop='1202',issued='2026-08-01T00:01:00-03:00'))
    upload(c,xml(6,80,fin='4',cfop='5202'))
    assert Decimal(summary(c)['net_amount'])==210 and Decimal(summary(c)['canceled_amount'])==100
    assert Decimal(c.get('/api/stats/summary?date_to=2026-07-31').json()['net_amount'])==250
    assert c.get('/api/stats/trends?granularity=month').json()==[{'period':'2026-07','amount':'250.00'},{'period':'2026-08','amount':'-40.00'}]
    upload(c,cancel(7));upload(c,xml(7,30));assert Decimal(summary(c)['net_amount'])==210
    upload(c,xml(8,1000,env='2'));assert Decimal(summary(c)['net_amount'])==210
    assert c.get('/api/invoices?environment=test').json()['total']==1

def test_conflict_keeps_primary_and_hold_statistics(client):
    c=client;upload(c,xml(1,100));report=upload(c,xml(1,101))
    assert report['counters']['conflict']==1
    assert Decimal(summary(c)['sales_amount'])==0 and Decimal(summary(c)['pending_amount'])==100
    invoice=c.get('/api/invoices').json()['items'][0]
    assert len(invoice['versions'])==2
    primary=next(v for v in invoice['versions'] if v['is_primary'])
    assert c.get(primary['download']).content==xml(1,100)
    assert c.post(f"/api/invoices/{invoice['id']}/resolve-conflict",json={'reason':'Synthetic original evidence retained'}).status_code==200
    assert Decimal(summary(c)['sales_amount'])==100

def test_whole_result_export_freezes_originals_and_revocation(client):
    c=client
    for n in range(1,4): upload(c,xml(n,n*100))
    job=c.post('/api/exports',json={'filters':{'min_amount':'100'},'all_versions':True}).json()
    assert c.get('/api/invoices?page_size=1').json()['total']==3
    upload(c,cancel(1))
    run_pending_once()
    done=c.get(f"/api/exports/{job['id']}").json()
    assert done['status']=='completed' and done['snapshot']['invoice_count']==3
    assert done['changes_since_snapshot']==[key(1)]
    with zipfile.ZipFile(io.BytesIO(c.get(done['download']).content)) as z:
        manifest=list(csv.DictReader(io.StringIO(z.read('manifest.csv').decode('utf-8-sig'))))
        assert len(manifest)==3
        for r in manifest: assert sha256(z.read(r['filename'])).hexdigest()==r['sha256']
    uid=c.post('/api/users',json={'login':'accountant','password':'Synthetic-accountant-123','role':'accountant','scopes':[{'company_id':1,'permissions':['view','export']}]}).json()['id']
    with TestClient(app) as ac:
        ac.post('/api/auth/login',json={'login':'accountant','password':'Synthetic-accountant-123'})
        private=ac.post('/api/exports',json={'filters':{}}).json();run_pending_once()
        c.patch(f'/api/users/{uid}',json={'login':'accountant','role':'accountant','scopes':[{'company_id':1,'permissions':['view']}]} )
        assert ac.get(f"/api/exports/{private['id']}/download").status_code==403
        assert ac.post('/api/imports',files={'files':('x.xml',xml(4,10))}).status_code==403
        assert ac.patch('/api/invoices/1/classification',json={'business_class':'sale','reason':'forbidden','company_id':1}).status_code==403
        assert ac.get('/api/companies?company_id=2').status_code==200
        assert ac.get('/api/invoices?company_id=2').status_code==403
    assert c.post('/api/saved-filters',json={'name':'July','filters':{'date_from':'2026-07-01'}}).status_code==200
    assert c.post('/api/exports',headers={'Origin':'https://evil.example'},json={'filters':{}}).status_code==403

def test_disk_full_retry_and_checkpoint_restart(client,monkeypatch):
    c=client;original=importer.save_object
    def full(*args,**kwargs): raise OSError(errno.ENOSPC,'No space left')
    monkeypatch.setattr(importer,'save_object',full)
    failed=upload(c,xml(1,100));assert failed['status']=='partial' and failed['counters']['failed']==1
    with SessionLocal() as db: assert db.scalar(select(func.count()).select_from(Invoice))==0
    monkeypatch.setattr(importer,'save_object',original)
    assert c.post(f"/api/imports/{failed['id']}/retry").status_code==200;run_pending_once()
    assert Decimal(summary(c)['sales_amount'])==100
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z: z.writestr('a.xml',xml(2,200));z.writestr('b.xml',xml(3,300))
    queued=upload(c,stream.getvalue(),'restart.zip',False)
    with SessionLocal() as db:
        task=db.scalar(select(DurableTask).where(DurableTask.kind=='import',DurableTask.entity_id==queued['id']))
        task.status='running';db.commit()
    run_pending_once();report=c.get(f"/api/imports/{queued['id']}").json()
    assert report['status']=='completed' and report['counters']['new']==2
    assert Decimal(summary(c)['sales_amount'])==600
    with SessionLocal() as db:
        for obj in db.scalars(select(FileObject)):
            assert sha256((settings.objects_dir/obj.relative_path).read_bytes()).hexdigest()==obj.sha256

def test_concurrent_duplicates_iso_week_and_internal_group(client):
    c=client;upload(c,xml(1,100,issued='2026-01-01T00:15:00+02:00'))
    first=upload(c,xml(1,100,issued='2026-01-01T00:15:00+02:00'),run=False)
    second=upload(c,xml(1,100,issued='2026-01-01T00:15:00+02:00'),run=False)
    with SessionLocal() as db: tasks=list(db.scalars(select(DurableTask.id).where(DurableTask.status=='queued')))
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(execute_task,tasks))
    assert Decimal(summary(c)['sales_amount'])==100
    assert c.get('/api/imports/'+str(first['id'])).json()['counters']['duplicate']==1
    assert c.get('/api/stats/trends?granularity=week').json()==[{'period':'2026-W01','amount':'100.00'}]
    c.post('/api/companies',json={'cnpj':OTHER,'confirmed':True})
    upload(c,xml(2,200,recipient=OTHER))
    assert Decimal(summary(c)['sales_amount'])==100
    assert Decimal(c.get('/api/stats/summary?company_id=1').json()['sales_amount'])==300
    assert c.get('/api/invoices?min_amount=200&max_amount=100').status_code==422
