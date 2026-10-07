"""Defensive NF-e model 55 parser with conservative fiscal evidence flags."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib, json, re
from typing import Any
from defusedxml import ElementTree as ET

_CNPJ_RE = re.compile(r"^[0-9A-Z]{14}$")
_KEY_RE = re.compile(r"^[0-9A-Z]{44}$")
_AUTH_STATUSES = {"100", "150"}
_CANCEL_STATUSES = {"135", "155"}

def lname(tag: str) -> str: return str(tag).rsplit("}", 1)[-1]
def children(node, name): return [x for x in list(node) if lname(x.tag) == name] if node is not None else []
def first(node, name):
    if node is None: return None
    return next((x for x in node.iter() if lname(x.tag) == name), None)
def text(node, name, default=""):
    x = first(node, name)
    return (x.text or "").strip() if x is not None and x.text is not None else default

def dec(value: str | None) -> Decimal:
    value = (value or "").strip()
    if not value: return Decimal("0")
    try:
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", value): raise ValueError("XML 金额/数量必须使用十进制数字和小数点")
        result = Decimal(value)
        if not result.is_finite(): raise ValueError("金额/数量不是有限数值")
        return result
    except (InvalidOperation, ValueError) as exc: raise ValueError("XML 金额/数量格式无效") from exc

def required_dec(value: str | None, field_name: str) -> Decimal:
    if value is None or not str(value).strip(): raise ValueError(f"缺少必需金额/数量字段 {field_name}")
    return dec(value)

def normalize_cnpj(value: str) -> str: return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()
def valid_cnpj(value: str) -> bool: return bool(_CNPJ_RE.fullmatch(normalize_cnpj(value)))

def normalize_access_key(value: str) -> str:
    value = re.sub(r"\s+", "", (value or "").strip().upper())
    if value.startswith("NFE"): value = value[3:]
    if not _KEY_RE.fullmatch(value): raise ValueError("访问键缺失或不是 44 位字母数字字符串")
    if value.isdigit():
        total = sum(int(ch) * (2 + i % 8) for i, ch in enumerate(reversed(value[:43])))
        rem = total % 11; expected = 0 if rem in (0, 1) else 11 - rem
        if int(value[-1]) != expected: raise ValueError("访问键校验位无效")
    return value

def parse_date(value: str) -> datetime | None:
    if not value: return None
    raw = value.strip()
    try: return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try: return datetime.strptime(raw[:10], fmt)
            except ValueError: pass
    return None

@dataclass
class ParsedItem:
    line_no: str; c_prod: str; x_prod: str; ncm: str; cfop: str; u_com: str
    q_com: Decimal; v_un_com: Decimal; v_prod: Decimal; v_desc: Decimal
    raw_taxes: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class ParsedEvent:
    access_key: str; event_type: str; sequence: str; accepted: bool
    receipt_status: str; protocol: str; event_at: datetime | None
    environment: str = "unknown"; issuer_cnpj: str = ""; raw: dict = field(default_factory=dict)
    request_present: bool = True; receipt_present: bool = False

@dataclass
class ParsedDocument:
    kind: str; access_key: str; environment: str; model: str; issuer_cnpj: str
    recipient_cnpj: str; issuer_name: str; number: str; series: str
    issue_at: datetime | None; issue_text: str; tp_nf: str; fin_nfe: str
    fiscal_status: str; v_nf: Decimal; v_prod: Decimal; v_desc: Decimal
    items: list[ParsedItem]; events: list[ParsedEvent]; raw_metadata: dict = field(default_factory=dict)
    @property
    def business_date(self): return self.issue_at.date() if self.issue_at else None
    def business_fingerprint(self) -> str:
        if self.raw_metadata.get("business_fingerprint"):
            return self.raw_metadata["business_fingerprint"]
        d = lambda v: format(v.normalize(), "f")
        def taxes(items):
            out = []
            for tax in items:
                vals = {}
                for k, v in sorted((tax.get("values") or {}).items()):
                    if k not in {"CST", "CSOSN", "orig", "modBC", "pRedBC"}:
                        try: vals[k] = d(Decimal(str(v).replace(",", ".")))
                        except (InvalidOperation, ValueError): vals[k] = v
                    else: vals[k] = v
                out.append((tax.get("type", ""), vals))
            return out
        p = {"env": self.environment, "key": self.access_key, "model": self.model,
             "issuer": self.issuer_cnpj, "recipient": self.recipient_cnpj,
             "number": self.number, "series": self.series, "issue": self.issue_at.isoformat() if self.issue_at else self.issue_text,
             "tpNF": self.tp_nf, "finNFe": self.fin_nfe, "vNF": d(self.v_nf),
             "vProd": d(self.v_prod), "vDesc": d(self.v_desc), "items": [
                 (i.line_no, i.c_prod, i.x_prod, i.ncm, i.cfop, i.u_com, d(i.q_com), d(i.v_un_com), d(i.v_prod), d(i.v_desc), taxes(i.raw_taxes))
                 for i in sorted(self.items, key=lambda x: x.line_no)]}
        p["references"] = self.raw_metadata.get("references", [])
        return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def _environment(tp_amb: str) -> str: return {"1": "production", "2": "test"}.get((tp_amb or "").strip(), "unknown")
def _key_from_inf(inf) -> str:
    return normalize_access_key((inf.attrib.get("Id", "") if inf is not None else "") or text(inf, "chNFe"))

def _protocol(root, expected_key: str, expected_env: str):
    prot = next((x for x in root.iter() if lname(x.tag) == "protNFe"), None)
    info = first(prot, "infProt") if prot is not None else None
    if info is None: return "", "", False
    cstat, key, env = text(info, "cStat"), text(info, "chNFe"), _environment(text(info, "tpAmb"))
    try: key = normalize_access_key(key)
    except ValueError: return cstat, text(info, "nProt"), False
    protocol = text(info, "nProt")
    credible = key == expected_key and env == expected_env and cstat in _AUTH_STATUSES and (not protocol or bool(re.fullmatch(r"\d+", protocol)))
    return cstat, protocol, credible

def _event_identity(info):
    if info is None: raise ValueError("事件缺少 infEvento")
    key = normalize_access_key(text(info, "chNFe"))
    typ, seq, env = text(info, "tpEvento"), text(info, "nSeqEvento"), _environment(text(info, "tpAmb"))
    if env == "unknown" or not re.fullmatch(r"\d{6}", typ) or not seq.isdigit() or not 1 <= int(seq) <= 999:
        raise ValueError("事件身份字段 tpAmb/tpEvento/nSeqEvento 缺失或无效")
    return env, key, typ, str(int(seq))

def parse_events(root) -> list[ParsedEvent]:
    """Match each request and receipt using the full event identity.

    Requests and receipts can also be uploaded as separate files.  An accepted
    receipt is sufficient evidence when it contains a complete identity; the
    import service validates its emitter against the linked invoice.
    """
    requests, receipts = {}, {}
    for container in root.iter():
        name = lname(container.tag)
        if name not in {"evento", "retEvento"}: continue
        info = next(iter(children(container, "infEvento")), None)
        identity = _event_identity(info)
        (requests if name == "evento" else receipts).setdefault(identity, []).append(info)
    out = []
    for identity in sorted(requests.keys() | receipts.keys()):
        env, key, typ, seq = identity
        reqs, rets = requests.get(identity, []), receipts.get(identity, [])
        req = reqs[0] if reqs else None
        # Preserve an accepted receipt when later nonaccepted receipts coexist.
        ret = next((r for r in rets if text(r, "cStat") in _CANCEL_STATUSES), rets[0] if rets else None)
        issuer = normalize_cnpj(text(req, "CNPJ")) or key[6:20]
        cstat, protocol = text(ret, "cStat"), text(ret, "nProt")
        accepted = ret is not None and cstat in _CANCEL_STATUSES and bool(re.fullmatch(r"\d+", protocol))
        out.append(ParsedEvent(key, typ, seq, accepted, cstat or "unknown", protocol,
                   parse_date(text(req, "dhEvento") or text(ret, "dhRegEvento")), env, issuer,
                   {"cStat": cstat, "request": bool(reqs), "receipt": bool(rets), "issuer_cnpj": issuer,
                    "request_receipt_matched": bool(reqs and rets)}, bool(reqs), bool(rets)))
    return out

def _canonical_business(node):
    """Complete infNFe content, normalized only for insignificant XML layout."""
    tag, val = lname(node.tag), (node.text or "").strip()
    if tag in {"CNPJ", "CPF"}: val = normalize_cnpj(val)
    if tag.startswith(("v", "p", "q")) and re.fullmatch(r"-?\d+(?:\.\d+)?", val):
        val = format(Decimal(val).normalize(), "f")
    attrs = sorted((lname(k), v) for k, v in node.attrib.items())
    return tag, attrs, val, [_canonical_business(x) for x in list(node)]

def parse_xml(raw: bytes) -> ParsedDocument:
    if not raw or len(raw) > 10 * 1024 * 1024: raise ValueError("XML 为空或超过 10 MiB 限制")
    try: root = ET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as exc: raise ValueError(f"XML 安全解析失败: {exc}") from exc
    root_name = lname(root.tag)
    allowed_roots = {"NFe", "nfeProc", "procEventoNFe", "evento", "retEvento", "envEvento", "retEnvEvento"}
    namespace = root.tag[1:].split("}", 1)[0] if str(root.tag).startswith("{") else ""
    if root_name not in allowed_roots or namespace not in {"", "http://www.portalfiscal.inf.br/nfe"}:
        return ParsedDocument("unsupported", "", "unknown", "", "", "", "", "", "", None, "", "", "", "unsupported", Decimal(0), Decimal(0), Decimal(0), [], [], {"root": root_name, "reason": "未知 XML 类型或命名空间"})
    events = parse_events(root)
    nfe = first(root, "NFe") if root_name not in {"NFe", "nfeProc"} else (first(root, "NFe") if first(root, "NFe") is not None else root)
    inf = first(nfe, "infNFe") if nfe is not None else None
    if inf is None:
        if events:
            ev = events[0]
            unsupported_event = any(e.event_type != "110111" for e in events)
            return ParsedDocument("unsupported" if unsupported_event else "event", ev.access_key, ev.environment, "55", ev.issuer_cnpj, "", "", "", "", None, "", "", "", "pending_evidence", Decimal(0), Decimal(0), Decimal(0), [], events, {"reason": "V1 仅处理取消事件 110111"} if unsupported_event else {})
        raise ValueError("未找到 infNFe，文件不是可识别的 NF-e XML")
    ide, emit, dest = first(inf, "ide"), first(inf, "emit"), first(inf, "dest"); model = text(ide, "mod")
    if model != "55": return ParsedDocument("unsupported", "", "unknown", model, "", "", "", "", "", None, "", "", "", "unsupported", Decimal(0), Decimal(0), Decimal(0), [], events, {"root": root_name, "reason": "仅支持 NF-e model 55"})
    schema_version = inf.attrib.get("versao") or root.attrib.get("versao")
    if schema_version and schema_version != "4.00":
        return ParsedDocument("unsupported", "", "unknown", model, "", "", "", "", "", None, "", "", "", "unsupported", Decimal(0), Decimal(0), Decimal(0), [], events, {"root": root_name, "reason": f"尚未支持 NF-e Schema {schema_version}"})
    key, env = _key_from_inf(inf), _environment(text(ide, "tpAmb"))
    if env == "unknown": raise ValueError("tpAmb 缺失或未知，无法隔离生产/测试环境")
    cstat, protocol, credible = _protocol(root, key, env)
    total, items, malformed = first(inf, "total"), [], []
    icms = first(total, "ICMSTot") if total is not None else None
    for det in children(inf, "det"):
        prod = first(det, "prod")
        if prod is None: continue
        vals = {k: text(prod, k) for k in ("qCom", "vUnCom", "vProd", "vDesc")}
        tax_node = first(det, "imposto"); raw_taxes = []
        if tax_node is not None:
            for tax in list(tax_node):
                raw_taxes.append({"type": lname(tax.tag), "values": {lname(x.tag): (x.text or "").strip() for x in tax.iter() if x is not tax and x.text and (x.text or "").strip()}})
        items.append(ParsedItem(det.attrib.get("nItem", str(len(items) + 1)), text(prod, "cProd"), text(prod, "xProd"), text(prod, "NCM"), text(prod, "CFOP"), text(prod, "uCom"), required_dec(vals["qCom"], "qCom"), required_dec(vals["vUnCom"], "vUnCom"), required_dec(vals["vProd"], "vProd"), dec(vals["vDesc"]), raw_taxes))
    refs = [text(x, "refNFe") for x in ide.iter() if lname(x.tag) == "NFref" and text(x, "refNFe")]
    semantic = json.dumps(_canonical_business(inf), ensure_ascii=False, separators=(",", ":"))
    meta = {"cStat": cstat, "nProt": protocol, "root": root_name, "authorized_evidence": credible, "schema_version": schema_version, "references": refs, "business_fingerprint": hashlib.sha256(semantic.encode()).hexdigest(), "header_taxes": {lname(x.tag): (x.text or "").strip() for x in list(icms)} if icms is not None else {}}
    issuer, recipient = normalize_cnpj(text(emit, "CNPJ") or text(emit, "CPF")), normalize_cnpj(text(dest, "CNPJ") or text(dest, "CPF"))
    if not valid_cnpj(issuer): raise ValueError("发行方 CNPJ 必须为 14 位字母数字字符串")
    if text(dest, "CNPJ") and not valid_cnpj(recipient): raise ValueError("接收方 CNPJ 必须为 14 位字母数字字符串")
    issue_text = text(ide, "dhEmi") or text(ide, "dEmi")
    issue_at = parse_date(issue_text)
    if not issue_at: raise ValueError("开票日期 dhEmi/dEmi 缺失或无效")
    return ParsedDocument("nfeProc" if root_name == "nfeProc" else "nfe", key, env, model, issuer, recipient, text(emit, "xNome"), text(ide, "nNF"), text(ide, "serie"), issue_at, issue_text, text(ide, "tpNF"), text(ide, "finNFe"), "authorized" if credible else "pending_evidence", required_dec(text(icms, "vNF"), "vNF"), required_dec(text(icms, "vProd"), "vProd"), dec(text(icms, "vDesc")), items, events, meta)
