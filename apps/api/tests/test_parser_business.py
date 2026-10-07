"""Business-boundary tests for the defensive parser (no real taxpayer data)."""
from decimal import Decimal
import io, zipfile
import pytest
from app.services.xml_parser import parse_xml, normalize_access_key, normalize_cnpj
from app.services.importer import iter_upload
from app.services.importer import classify
from app.models import Company, Invoice

def key43(prefix="3526101234567800019955001000000001"):
    body = (prefix + "0" * 43)[:43]
    total = sum(int(ch) * (2 + i % 8) for i, ch in enumerate(reversed(body)))
    rem = total % 11
    return body + str(0 if rem in (0, 1) else 11 - rem)

def xml(key=None, root="NFe", status=None, model="55", tp="1", fin="1", cfop="5102", date="2026-07-01T23:30:00-03:00"):
    key = key or key43()
    prot = f"<protNFe><infProt><tpAmb>1</tpAmb><chNFe>{key}</chNFe><cStat>{status}</cStat><nProt>123</nProt></infProt></protNFe>" if status else ""
    body = f'''<NFe xmlns="http://www.portalfiscal.inf.br/nfe"><infNFe Id="NFe{key}"><ide><tpAmb>1</tpAmb><mod>{model}</mod><serie>1</serie><nNF>1</nNF><dhEmi>{date}</dhEmi><tpNF>{tp}</tpNF><finNFe>{fin}</finNFe></ide><emit><CNPJ>12.345.678/0001-99</CNPJ><xNome>Emitter</xNome></emit><dest><CNPJ>98.765.432/0001-11</CNPJ></dest><det nItem="1"><prod><cProd>SKU-1</cProd><xProd>Produto</xProd><CFOP>{cfop}</CFOP><qCom>1</qCom><vUnCom>10.00</vUnCom><vProd>10.00</vProd></prod></det><total><ICMSTot><vNF>10.00</vNF><vProd>10.00</vProd><vDesc>0.00</vDesc></ICMSTot></total></infNFe></NFe>{prot}'''
    return (f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">{body}</nfeProc>' if (root == "nfeProc" or status) else body).encode()

def test_model55_authorization_requires_matching_protocol():
    p = parse_xml(xml(status="100")); assert p.fiscal_status == "authorized" and p.environment == "production"
    assert parse_xml(xml(status=None)).fiscal_status == "pending_evidence"

def test_bad_protocol_key_does_not_authorize():
    raw = xml(status="100").replace(key43().encode(), key43("3526101234567800019955001000000002").encode(), 1)
    assert parse_xml(raw).fiscal_status == "pending_evidence"

def test_invalid_access_key_rejected_and_unknown_model_held():
    with pytest.raises(ValueError): parse_xml(xml(key="1" * 44))
    assert parse_xml(xml(model="65")).kind == "unsupported"

def test_decimal_and_source_offset_local_date():
    p = parse_xml(xml(status="100", date="2026-07-31T23:30:00-03:00")); assert p.business_date.isoformat() == "2026-07-31"; assert p.v_nf == Decimal("10.00")

def test_dtd_external_entity_rejected():
    with pytest.raises(ValueError): parse_xml(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><NFe>&xxe;</NFe>')

def test_event_acceptance_only_135_or_155_and_paired_key():
    k = key43(); raw = f'''<procEventoNFe xmlns="http://www.portalfiscal.inf.br/nfe"><evento><infEvento><tpAmb>1</tpAmb><CNPJ>12345678000199</CNPJ><chNFe>{k}</chNFe><tpEvento>110111</tpEvento><nSeqEvento>1</nSeqEvento><dhEvento>2026-07-02T12:00:00-03:00</dhEvento></infEvento></evento><retEvento><infEvento><tpAmb>1</tpAmb><chNFe>{k}</chNFe><tpEvento>110111</tpEvento><nSeqEvento>1</nSeqEvento><nProt>123456789012345</nProt><cStat>136</cStat></infEvento></retEvento></procEventoNFe>'''.encode()
    assert parse_xml(raw).events[0].accepted is False
    assert parse_xml(raw.replace(b"136", b"135")).events[0].accepted is True

def test_zip_safety_limits_and_path_traversal():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z: z.writestr("../escape.xml", xml())
    with pytest.raises(ValueError): list(iter_upload(b.getvalue(), "x.zip"))

def test_cnpj_normalization_preserves_alphanumeric_and_leading_zero():
    assert normalize_cnpj("00.abc/0001-0z") == "00ABC00010Z"; assert normalize_access_key(key43()).isalnum()

def test_classification_is_conservative_for_transfer_cfop_and_returns():
    company = Company(cnpj="12345678000199", confirmed=True)
    inv = Invoice(fiscal_status="authorized")
    sale = parse_xml(xml(status="100", cfop="5102")); assert classify(sale, company, inv) == ("sale", True)
    transfer = parse_xml(xml(status="100", cfop="5105")); assert classify(transfer, company, inv) == ("pending", False)
    returned = parse_xml(xml(status="100", cfop="1202", tp="0", fin="4")); assert classify(returned, company, inv) == ("sales_return", True)
