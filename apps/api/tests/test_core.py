import io, os, zipfile
from fastapi.testclient import TestClient


from app.main import app
from app.db import Base, engine
from app.services.jobs import run_pending_once

client = TestClient(app)
XML = '''<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe35261012345678000199550010000000011000000019"><ide><tpAmb>1</tpAmb><mod>55</mod><serie>1</serie><nNF>1</nNF><dhEmi>2026-10-01T10:00:00-03:00</dhEmi><tpNF>1</tpNF><finNFe>1</finNFe></ide><emit><CNPJ>12.345.678/0001-99</CNPJ><xNome>Venda Teste</xNome></emit><dest><CNPJ>98.765.432/0001-11</CNPJ></dest><det nItem="1"><prod><cProd>SKU-1</cProd><xProd>Produto Um</xProd><NCM>1234</NCM><CFOP>5102</CFOP><uCom>UN</uCom><qCom>2</qCom><vUnCom>50</vUnCom><vProd>100</vProd></prod></det><total><ICMSTot><vProd>100</vProd><vNF>100</vNF><vDesc>0</vDesc></ICMSTot></total></infNFe></NFe><protNFe><infProt><tpAmb>1</tpAmb><chNFe>35261012345678000199550010000000011000000019</chNFe><cStat>100</cStat><nProt>1</nProt></infProt></protNFe></nfeProc>'''.encode()

def setup_module():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    client.post('/api/auth/bootstrap', json={'login':'admin','password':'password-12345','display_name':'Admin'})
    client.post('/api/auth/login', json={'login':'admin','password':'password-12345'})
    client.post('/api/companies', json={'cnpj':'12.345.678/0001-99','legal_name':'Venda Teste','confirmed':True})

def test_import_is_idempotent_and_stats():
    r = client.post('/api/imports', files={'files':('a.xml',XML,'application/xml')}); assert r.status_code == 200; run_pending_once(); assert client.get(f"/api/imports/{r.json()['id']}").json()['counters']['new'] == 1
    r = client.post('/api/imports', files={'files':('renamed.xml',XML,'application/xml')}); assert r.status_code == 200; run_pending_once(); assert client.get(f"/api/imports/{r.json()['id']}").json()['counters']['duplicate'] == 1
    s = client.get('/api/stats/summary').json(); assert s['sales_amount'] == '100.00'; assert s['invoice_count'] == 1
    assert client.get('/api/invoices?sku=SKU-1&product_name=NoExiste').json()['items'] == []
    assert len(client.get('/api/invoices?sku=SKU-1&product_name=Produto').json()['items']) == 1

def test_zip_path_traversal_rejected():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z: z.writestr('../evil.xml', XML)
    r = client.post('/api/imports', files={'files':('bad.zip',stream.getvalue(),'application/zip')}); assert r.status_code == 200
    run_pending_once(); assert client.get(f"/api/imports/{r.json()['id']}").json()['counters']['failed'] >= 1

def test_export_contains_manifest_and_original_bytes():
    r = client.post('/api/exports', json={'filters': {'sku': 'SKU-1'}})
    assert r.status_code == 200
    run_pending_once()
    job = client.get(f"/api/exports/{r.json()['id']}").json()
    assert job['status'] == 'completed', job['snapshot'].get('error')
    archive = client.get(job['download'])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert 'manifest.csv' in z.namelist()
        xml_names = [n for n in z.namelist() if n.endswith('.xml')]
        assert len(xml_names) == 1
        assert z.read(xml_names[0]) == XML
