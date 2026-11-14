import os
import json
import hashlib
import base64
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET
from xml.dom import minidom
import streamlit as st

# Tentativa opcional de carregar cryptography e google-genai (não trava o app se ausentes)
try:
    from cryptography.hazmat.primitives.serialization import pkcs12
    CRYPTO_DISPONIVEL = True
except ImportError:
    CRYPTO_DISPONIVEL = False

try:
    from google import genai
    from google.genai import types
    GENAI_DISPONIVEL = True
except ImportError:
    GENAI_DISPONIVEL = False

# =====================================================================
# CONFIGURAÇÃO DA PÁGINA STREAMLIT
# =====================================================================
st.set_page_config(
    page_title="Auditor & Agente RTC 2026 - Receita Federal",
    page_icon="🏛️",
    layout="wide"
)

NFE_NS = "http://www.portalfiscal.inf.br/nfe"
NFSE_NS = "http://www.sped.fazenda.gov.br/nfse"

TABELA_CCLASS_VALIDOS = {
    "000": {
        "000001": {"descricao": "Tributação Integral - Regime Regular (Art. 4º LC 214/2025)", "redutor": Decimal("0.00")},
    },
    "200": {
        "200030": {"descricao": "Redução de 30% - Profissões Intelectuais Regulamentadas", "redutor": Decimal("30.00")},
        "200060": {"descricao": "Redução de 60% - Serviços de Saúde Humana e Médicos (Art. 130 LC 214)", "redutor": Decimal("60.00")},
        "200061": {"descricao": "Redução de 60% - Serviços de Educação (Art. 129 LC 214)", "redutor": Decimal("60.00")},
        "200065": {"descricao": "Redução de 60% - Insumos Agropecuários e Hortifruti", "redutor": Decimal("60.00")},
    },
    "300": {
        "300001": {"descricao": "Alíquota Zero - Cesta Básica Nacional de Alimentos", "redutor": Decimal("100.00")},
    },
    "410": {
        "410001": {"descricao": "Imunidade / Não Incidência / Exportação", "redutor": Decimal("100.00")},
    }
}


def _d(valor: float | str | Decimal, casas: str = "0.01") -> Decimal:
    return Decimal(str(valor)).quantize(Decimal(casas), rounding=ROUND_HALF_UP)


def formatar_xml_nativo(elem: ET.Element) -> str:
    """Formata o XML com indentação usando apenas bibliotecas nativas do Python."""
    xml_bruto = ET.tostring(elem, encoding="utf-8")
    reparsed = minidom.parseString(xml_bruto)
    return "\n".join([
        linha for linha in reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8").splitlines()
        if linha.strip()
    ])


# =====================================================================
# MOTOR NATIVO DE GERAÇÃO E AUDITORIA XML (ZERO LXML)
# =====================================================================

def gerar_xml_ibscbs_nativo(
    modelo_doc: str,
    chave_id: str,
    v_bc_float: float,
    cst: str,
    c_class_trib: str,
    aliq_ibs_uf: float,
    aliq_ibs_mun: float,
    aliq_cbs: float,
    crt: int
) -> tuple[str, dict]:
    v_bc = _d(v_bc_float, "0.01")
    p_ibs_uf = _d(aliq_ibs_uf, "0.0001")
    p_ibs_mun = _d(aliq_ibs_mun, "0.0001")
    p_cbs = _d(aliq_cbs, "0.0001")

    info_class = TABELA_CCLASS_VALIDOS.get(cst, {}).get(c_class_trib)
    p_red = info_class["redutor"] if info_class else Decimal("0.00")
    fator = (Decimal("100.00") - p_red) / Decimal("100.00")

    p_efet_uf = _d(p_ibs_uf * fator, "0.0001")
    p_efet_mun = _d(p_ibs_mun * fator, "0.0001")
    p_efet_cbs = _d(p_cbs * fator, "0.0001")

    v_ibs_uf = _d(v_bc * (p_efet_uf / Decimal("100")), "0.01")
    v_ibs_mun = _d(v_bc * (p_efet_mun / Decimal("100")), "0.01")
    v_ibs_tot = _d(v_ibs_uf + v_ibs_mun, "0.01")
    v_cbs = _d(v_bc * (p_efet_cbs / Decimal("100")), "0.01")

    if modelo_doc == "NF-e 4.00 (Modelo 55/65)":
        nfe = ET.Element("NFe", xmlns=NFE_NS)
        inf_nfe = ET.SubElement(nfe, "infNFe", Id=chave_id, versao="4.00")

        emit = ET.SubElement(inf_nfe, "emit")
        ET.SubElement(emit, "CRT").text = str(crt)

        det = ET.SubElement(inf_nfe, "det", nItem="1")
        imposto = ET.SubElement(det, "imposto")
        ibscbs = ET.SubElement(imposto, "IBSCBS")
        ET.SubElement(ibscbs, "CST").text = cst
        ET.SubElement(ibscbs, "cClassTrib").text = c_class_trib

        g_ibscbs = ET.SubElement(ibscbs, "gIBSCBS")
        ET.SubElement(g_ibscbs, "vBC").text = f"{v_bc:.2f}"

        g_ibs_uf = ET.SubElement(g_ibscbs, "gIBSUF")
        ET.SubElement(g_ibs_uf, "pIBSUF").text = f"{p_ibs_uf:.4f}"
        if p_red > Decimal("0.00"):
            g_red_uf = ET.SubElement(g_ibs_uf, "gRed")
            ET.SubElement(g_red_uf, "pRedAliq").text = f"{p_red:.2f}"
            ET.SubElement(g_red_uf, "pAliqEfet").text = f"{p_efet_uf:.4f}"
        ET.SubElement(g_ibs_uf, "vIBSUF").text = f"{v_ibs_uf:.2f}"

        g_ibs_mun = ET.SubElement(g_ibscbs, "gIBSMun")
        ET.SubElement(g_ibs_mun, "pIBSMun").text = f"{p_ibs_mun:.4f}"
        if p_red > Decimal("0.00"):
            g_red_mun = ET.SubElement(g_ibs_mun, "gRed")
            ET.SubElement(g_red_mun, "pRedAliq").text = f"{p_red:.2f}"
            ET.SubElement(g_red_mun, "pAliqEfet").text = f"{p_efet_mun:.4f}"
        ET.SubElement(g_ibs_mun, "vIBSMun").text = f"{v_ibs_mun:.2f}"

        g_cbs = ET.SubElement(g_ibscbs, "gCBS")
        ET.SubElement(g_cbs, "pCBS").text = f"{p_cbs:.4f}"
        if p_red > Decimal("0.00"):
            g_red_cbs = ET.SubElement(g_cbs, "gRed")
            ET.SubElement(g_red_cbs, "pRedAliq").text = f"{p_red:.2f}"
            ET.SubElement(g_red_cbs, "pAliqEfet").text = f"{p_efet_cbs:.4f}"
        ET.SubElement(g_cbs, "vCBS").text = f"{v_cbs:.2f}"

        total = ET.SubElement(inf_nfe, "total")
        tot_ibscbs = ET.SubElement(total, "IBSCBSTot")
        ET.SubElement(tot_ibscbs, "vBCIBSCBS").text = f"{v_bc:.2f}"

        tot_g_ibs = ET.SubElement(tot_ibscbs, "gIBS")
        ET.SubElement(ET.SubElement(tot_g_ibs, "gIBSUF"), "vIBSUF").text = f"{v_ibs_uf:.2f}"
        ET.SubElement(ET.SubElement(tot_g_ibs, "gIBSMun"), "vIBSMun").text = f"{v_ibs_mun:.2f}"
        ET.SubElement(tot_g_ibs, "vIBS").text = f"{v_ibs_tot:.2f}"

        ET.SubElement(ET.SubElement(tot_ibscbs, "gCBS"), "vCBS").text = f"{v_cbs:.2f}"
        xml_final = formatar_xml_nativo(nfe)
    else:
        dps = ET.Element("DPS", xmlns=NFSE_NS, versao="1.01")
        inf_dps = ET.SubElement(dps, "infDPS", Id=chave_id)
        ibscbs = ET.SubElement(ET.SubElement(ET.SubElement(inf_dps, "valores"), "trib"), "IBSCBS")
        ET.SubElement(ibscbs, "CST").text = cst
        ET.SubElement(ibscbs, "cClassTrib").text = c_class_trib
        ET.SubElement(ibscbs, "vBC").text = f"{v_bc:.2f}"
        ET.SubElement(ibscbs, "pIBSUF").text = f"{p_efet_uf:.4f}"
        ET.SubElement(ibscbs, "vIBSUF").text = f"{v_ibs_uf:.2f}"
        ET.SubElement(ibscbs, "pIBSMun").text = f"{p_efet_mun:.4f}"
        ET.SubElement(ibscbs, "vIBSMun").text = f"{v_ibs_mun:.2f}"
        ET.SubElement(ibscbs, "pCBS").text = f"{p_efet_cbs:.4f}"
        ET.SubElement(ibscbs, "vCBS").text = f"{v_cbs:.2f}"
        xml_final = formatar_xml_nativo(dps)

    resumo = {
        "vBC": f"R$ {v_bc:.2f}",
        "pEfetIBS": f"{(p_efet_uf + p_efet_mun):.4f}%",
        "vIBS": f"R$ {v_ibs_tot:.2f}",
        "pEfetCBS": f"{p_efet_cbs:.4f}%",
        "vCBS": f"R$ {v_cbs:.2f}",
        "Total_IVA": f"R$ {(v_ibs_tot + v_cbs):.2f}"
    }
    return xml_final, resumo


def validar_xml_nativo_sefaz(xml_str: str) -> dict:
    erros = []
    try:
        root = ET.fromstring(xml_str.encode("utf-8"))
    except Exception as e:
        return {"valido": False, "rejeicoes": [f"Falha de sintaxe XML: {e}"]}

    # Remove namespaces das tags para facilitar a busca nativa
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    blocos_ibscbs = root.findall(".//det/imposto/IBSCBS") or root.findall(".//valores/trib/IBSCBS")
    if not blocos_ibscbs:
        return {"valido": False, "rejeicoes": ["Rejeição 1115: Grupo IBSCBS obrigatório não informado no documento."]}

    soma_vbc = Decimal("0.00")
    soma_ibs_uf = Decimal("0.00")
    soma_ibs_mun = Decimal("0.00")
    soma_cbs = Decimal("0.00")

    for idx, ibscbs in enumerate(blocos_ibscbs, start=1):
        cst = (ibscbs.findtext("CST") or "").strip()
        c_class = (ibscbs.findtext("cClassTrib") or "").strip()

        if cst not in TABELA_CCLASS_VALIDOS:
            erros.append(f"Item {idx} — Rejeição 1022: CST '{cst}' inválido para IBS/CBS.")
        elif len(c_class) != 6 or not c_class.startswith(cst):
            erros.append(
                f"Item {idx} — Rejeição 1023: cClassTrib '{c_class}' incompatível com CST '{cst}'."
            )

        g_ibscbs = ibscbs.find("gIBSCBS")
        if g_ibscbs is not None:
            v_bc = _d(g_ibscbs.findtext("vBC") or "0.00")
            p_uf = _d(g_ibscbs.findtext("gIBSUF/pIBSUF") or "0.0000", "0.0001")
            v_uf = _d(g_ibscbs.findtext("gIBSUF/vIBSUF") or "0.00")
            p_mun = _d(g_ibscbs.findtext("gIBSMun/pIBSMun") or "0.0000", "0.0001")
            v_mun = _d(g_ibscbs.findtext("gIBSMun/vIBSMun") or "0.00")
            p_cbs = _d(g_ibscbs.findtext("gCBS/pCBS") or "0.0000", "0.0001")
            v_cbs = _d(g_ibscbs.findtext("gCBS/vCBS") or "0.00")

            p_efet_uf = _d(g_ibscbs.findtext("gIBSUF/gRed/pAliqEfet") or str(p_uf), "0.0001")
            p_efet_cbs = _d(g_ibscbs.findtext("gCBS/gRed/pAliqEfet") or str(p_cbs), "0.0001")

            if (p_uf + p_mun) != Decimal("0.1000") and cst in ("000", "200"):
                erros.append(f"Item {idx} — Rejeição 1026: Soma de pIBSUF + pIBSMun ({p_uf + p_mun}%) difere de 0,1000% (ano-teste 2026).")
            if p_cbs != Decimal("0.9000") and cst in ("000", "200"):
                erros.append(f"Item {idx} — Rejeição 1037: Alíquota pCBS ({p_cbs}%) difere de 0,9000% (ano-teste 2026).")

            esp_uf = _d(v_bc * (p_efet_uf / Decimal("100")))
            if abs(v_uf - esp_uf) > Decimal("0.01"):
                erros.append(f"Item {idx} — Rejeição 1041: vIBSUF informado ({v_uf}) difere do calculado ({esp_uf}).")

            esp_cbs = _d(v_bc * (p_efet_cbs / Decimal("100")))
            if abs(v_cbs - esp_cbs) > Decimal("0.01"):
                erros.append(f"Item {idx} — Rejeição 1043: vCBS informado ({v_cbs}) difere do calculado ({esp_cbs}).")

            soma_vbc += v_bc
            soma_ibs_uf += v_uf
            soma_ibs_mun += v_mun
            soma_cbs += v_cbs

    if root.tag == "NFe" or root.find(".//infNFe") is not None:
        tot = root.find(".//total/IBSCBSTot")
        if tot is None:
            erros.append("Rejeição 349: Grupo totalizador <IBSCBSTot> ausente na NF-e.")
        else:
            tot_vbc = _d(tot.findtext("vBCIBSCBS") or "0.00")
            tot_ibs = _d(tot.findtext("gIBS/vIBS") or "0.00")
            tot_cbs = _d(tot.findtext("gCBS/vCBS") or "0.00")
            if tot_vbc != soma_vbc:
                erros.append(f"Rejeição 349 (Base): vBCIBSCBS ({tot_vbc}) difere da soma dos itens ({soma_vbc}).")
            if tot_ibs != (soma_ibs_uf + soma_ibs_mun):
                erros.append(f"Rejeição 349 (IBS): vIBS total ({tot_ibs}) difere da soma UF+Mun ({soma_ibs_uf + soma_ibs_mun}).")
            if tot_cbs != soma_cbs:
                erros.append(f"Rejeição 349 (CBS): vCBS total ({tot_cbs}) difere da soma dos itens ({soma_cbs}).")

    return {"valido": len(erros) == 0, "rejeicoes": erros}


# =====================================================================
# BARRA LATERAL (CERTIFICADO E-CNPJ + GEMINI API KEY)
# =====================================================================
with st.sidebar:
    st.header("🔐 Credenciais & mTLS")
    api_key = st.text_input("Gemini API Key (`gemini-4-argon`)", type="password", value=os.getenv("GEMINI_API_KEY", ""))
    st.divider()
    st.subheader("Certificado Digital e-CNPJ (A1)")
    arquivo_pfx = st.file_uploader("Upload do arquivo .pfx / .p12", type=["pfx", "p12"])
    senha_pfx = st.text_input("Senha do Certificado", type="password")

    if arquivo_pfx and CRYPTO_DISPONIVEL:
        try:
            priv_key, cert, _ = pkcs12.load_key_and_certificates(
                arquivo_pfx.getvalue(),
                senha_pfx.encode("utf-8") if senha_pfx else None
            )
            st.success(f"Certificado ICP-Brasil válido!\nTitular: {cert.subject.rfc4514_string()[:65]}...")
            st.caption(f"Expira em: {cert.not_valid_after_utc.strftime('%d/%m/%Y')}")
        except Exception as e:
            st.error(f"Erro ao abrir .pfx: {e}")

# =====================================================================
# INTERFACE PRINCIPAL EM ABAS
# =====================================================================
st.title("🏛️ Bancada Fiscal RTC 2026 — Receita Federal & SEFIN Nacional")
st.caption("Motor 100% nativo para Streamlit (sem dependência obrigatória de lxml) integrado ao Gemini 4 Argon")

aba_gerador, aba_validador, aba_agente = st.tabs([
    "📄 1. Gerador XML (IBS / CBS)",
    "✅ 2. Validador SEFAZ / NT 2025.002",
    "🤖 3. Agente Tributário (Gemini 4 Argon)"
])

with aba_gerador:
    col1, col2 = st.columns([1, 1.3])
    with col1:
        modelo_doc = st.selectbox("Modelo de Documento Fiscal", ["NF-e 4.00 (Modelo 55/65)", "DPS / NFS-e Nacional (ADN)"])
        crt_op = st.selectbox("Regime Tributário (CRT)", [3, 1], format_func=lambda x: "3 - Regime Normal" if x == 3 else "1 - Simples Nacional")
        cst_sel = st.selectbox("CST (IBS/CBS)", list(TABELA_CCLASS_VALIDOS.keys()))
        opcoes_cclass = list(TABELA_CCLASS_VALIDOS[cst_sel].keys())
        cclass_sel = st.selectbox(
            "cClassTrib (LC 214/2025)",
            opcoes_cclass,
            format_func=lambda c: f"{c} — {TABELA_CCLASS_VALIDOS[cst_sel][c]['descricao']}"
        )
        v_bc_input = st.number_input("Base de Cálculo da Operação (vBC R$)", min_value=1.0, value=2500.0, step=100.0)

        c_a1, c_a2, c_a3 = st.columns(3)
        aliq_uf = c_a1.number_input("pIBSUF (%)", value=0.0500, format="%.4f")
        aliq_mun = c_a2.number_input("pIBSMun (%)", value=0.0500, format="%.4f")
        aliq_cbs = c_a3.number_input("pCBS (%)", value=0.9000, format="%.4f")

        chave_padrao = "NFe35261012345678000199550010000001001000001000" if "NF-e" in modelo_doc else "DPS35522052123456780001990000100000000000001"
        chave_id = st.text_input("Atributo Id Assinável", value=chave_padrao)

    with col2:
        if crt_op == 1 and "DPS" in modelo_doc:
            st.warning("⚠️ **Ato Conjunto RFB/CGIBS nº 4/2026:** Optantes pelo Simples Nacional (CRT=1) estão dispensados de enviar o grupo IBSCBS em 2026. O envio na DPS gera rejeição na ADN.")
        xml_gerado, resumo_calc = gerar_xml_ibscbs_nativo(
            modelo_doc, chave_id, v_bc_input, cst_sel, cclass_sel, aliq_uf, aliq_mun, aliq_cbs, crt_op
        )
        st.session_state["ultimo_xml"] = xml_gerado

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Base (vBC)", resumo_calc["vBC"])
        m2.metric(f"IBS ({resumo_calc['pEfetIBS']})", resumo_calc["vIBS"])
        m3.metric(f"CBS ({resumo_calc['pEfetCBS']})", resumo_calc["vCBS"])
        m4.metric("Total IVA Dual", resumo_calc["Total_IVA"])

        st.code(xml_gerado, language="xml")
        st.download_button("⬇️ Baixar XML Gerado", data=xml_gerado, file_name="dfe_rtc_2026.xml", mime="application/xml")

with aba_validador:
    st.subheader("Auditoria de Regras de Validação (RV) e Digest SHA-256")
    xml_para_validar = st.text_area(
        "Cole ou edite o XML abaixo para simular rejeições da SEFAZ (ex: altere vCBS ou remova IBSCBSTot):",
        value=st.session_state.get("ultimo_xml", ""),
        height=320
    )
    if st.button("🔍 Auditar XML nas Regras da SEFAZ / Receita Federal", type="primary"):
        resultado = validar_xml_nativo_sefaz(xml_para_validar)
        digest_sha256 = base64.b64encode(hashlib.sha256(xml_para_validar.strip().encode("utf-8")).digest()).decode("utf-8")

        if resultado["valido"]:
            st.success(f"✅ **XML 100% Aprovado!** Nenhuma rejeição encontrada nas regras de 2026.\n\n`DigestValue (SHA-256 C14N): {digest_sha256}`")
        else:
            st.error(f"❌ **Foram encontradas {len(resultado['rejeicoes'])} Rejeições Fiscais:**")
            for rej in resultado["rejeicoes"]:
                st.markdown(f"- **{rej}**")

with aba_agente:
    st.subheader("Consultor Jurídico e Fiscal — Gemini 4 Argon")
    pergunta = st.text_area(
        "Consulte o enquadramento na LC 214/2025, códigos cClassTrib ou regras de apuração da CBS:",
        placeholder="Ex: Qual o CST e cClassTrib para serviços médicos de clínica capilar em Sorocaba/SP em 2026 e como funciona a compensação de 0,9% da CBS?"
    )
    if st.button("Consultar Gemini 4 Argon"):
        if not GENAI_DISPONIVEL:
            st.error("Instale o SDK com: `python -m pip install google-genai`")
        elif not api_key:
            st.warning("Informe sua `GEMINI_API_KEY` na barra lateral esquerda.")
        else:
            with st.spinner("Gemini 4 Argon analisando a LC 214/2025 e normas da Receita Federal..."):
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model="gemini-4-argon",
                    contents=pergunta,
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            "Você é um Auditor Fiscal e Especialista na Reforma Tributária (LC 214/2025, "
                            "LC 227/2026 e Notas Técnicas 2025.002-RTC e NFS-e Nacional). Responda com precisão "
                            "citando os artigos da LC 214/2025, o CST, o cClassTrib e o impacto no XML."
                        ),
                        temperature=0.1
                    )
                )
                st.markdown(resp.text)
