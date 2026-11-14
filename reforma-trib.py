import os
import json
from decimal import Decimal, ROUND_HALF_UP
from lxml import etree
from signxml import XMLSigner, methods
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from google import genai
from google.genai import types

NFE_NS = "http://www.portalfiscal.inf.br/nfe"
NFSE_NS = "http://www.sped.fazenda.gov.br/nfse"

# Tabela de referência de combinações válidas CST x cClassTrib (LC 214/2025 - Anexo de Classificação)
TABELA_CCLASS_VALIDOS = {
    "000": {
        "000001": {"descricao": "Tributação Integral pelo Regime Regular", "redutor_perc": Decimal("0.00")},
    },
    "200": {
        "200030": {"descricao": "Redução de 30% - Profissões Intelectuais Regulamentadas", "redutor_perc": Decimal("30.00")},
        "200060": {"descricao": "Redução de 60% - Serviços de Saúde Humana e Médicos (Art. 130 LC 214)", "redutor_perc": Decimal("60.00")},
        "200061": {"descricao": "Redução de 60% - Serviços de Educação (Art. 129 LC 214)", "redutor_perc": Decimal("60.00")},
        "200065": {"descricao": "Redução de 60% - Insumos Agropecuários e Hortifruti", "redutor_perc": Decimal("60.00")},
    },
    "300": {
        "300001": {"descricao": "Alíquota Zero - Cesta Básica Nacional de Alimentos", "redutor_perc": Decimal("100.00")},
    },
    "410": {
        "410001": {"descricao": "Imunidade ou Não Incidência Constitucional / Exportação", "redutor_perc": Decimal("100.00")},
    }
}


def _d(valor: float | str | Decimal, casas: str = "0.01") -> Decimal:
    """Arredondamento fiscal ABNT/SEFAZ com precisão decimal exata."""
    return Decimal(str(valor)).quantize(Decimal(casas), rounding=ROUND_HALF_UP)


# =====================================================================
# TOOL 1: GERADOR DE TAGS XML IBS/CBS (NF-e 4.00 e DPS NFS-e Nacional)
# =====================================================================

def gerar_xml_ibscbs_dfe(
    modelo_documento: str,
    chave_id: str,
    valor_base_calculo: float,
    cst: str,
    c_class_trib: str,
    aliquota_ibs_uf: float = 0.10,
    aliquota_ibs_mun: float = 0.00,
    aliquota_cbs: float = 0.90,
    crt_emitente: int = 3
) -> str:
    """
    Gera o XML estruturado com os grupos IBSCBS (item) e IBSCBSTot (total) segundo
    a Nota Técnica 2025.002-RTC (NF-e/NFC-e) ou NT 004/007 (DPS NFS-e Nacional).

    Args:
        modelo_documento: 'NFE' (Modelo 55/65) ou 'NFSE_DPS' (Padrão Nacional ADN/SEFIN).
        chave_id: Identificador da tag assinável (ex: 'NFe352610...' ou 'DPS3552205...').
        valor_base_calculo: Base de cálculo da operação em R$ (vBC).
        cst: Código de Situação Tributária de 3 dígitos (ex: '000', '200').
        c_class_trib: Código de Classificação Tributária de 6 dígitos (ex: '000001', '200060').
        aliquota_ibs_uf: Alíquota nominal do IBS Estadual em % (padrão 2026: 0.10 ou 0.05).
        aliquota_ibs_mun: Alíquota nominal do IBS Municipal em % (padrão 2026: 0.00 ou 0.05).
        aliquota_cbs: Alíquota nominal da CBS Federal em % (padrão 2026: 0.90).
        crt_emitente: Código de Regime Tributário (1=Simples Nacional, 3=Regime Normal).
    """
    if crt_emitente == 1 and modelo_documento.upper() == "NFSE_DPS":
        return json.dumps({
            "status": "bloqueio_preventivo",
            "motivo": (
                "Pelo Ato Conjunto RFB/CGIBS nº 4/2026 e regras da ADN, prestadores do Simples Nacional "
                "(CRT=1) não devem enviar o grupo IBSCBS na DPS em 2026 sob pena de rejeição."
            )
        }, ensure_ascii=False)

    v_bc = _d(valor_base_calculo, "0.01")
    p_ibs_uf = _d(aliquota_ibs_uf, "0.0001")
    p_ibs_mun = _d(aliquota_ibs_mun, "0.0001")
    p_cbs = _d(aliquota_cbs, "0.0001")

    # Verifica se há redução de alíquota vinculada ao CST/cClassTrib
    info_class = TABELA_CCLASS_VALIDOS.get(cst, {}).get(c_class_trib)
    p_red = info_class["redutor_perc"] if info_class else Decimal("0.00")
    fator = (Decimal("100.00") - p_red) / Decimal("100.00")

    p_efet_uf = _d(p_ibs_uf * fator, "0.0001")
    p_efet_mun = _d(p_ibs_mun * fator, "0.0001")
    p_efet_cbs = _d(p_cbs * fator, "0.0001")

    v_ibs_uf = _d(v_bc * (p_efet_uf / Decimal("100")), "0.01")
    v_ibs_mun = _d(v_bc * (p_efet_mun / Decimal("100")), "0.01")
    v_ibs_tot = _d(v_ibs_uf + v_ibs_mun, "0.01")
    v_cbs = _d(v_bc * (p_efet_cbs / Decimal("100")), "0.01")

    if modelo_documento.upper() == "NFE":
        nsmap = {None: NFE_NS}
        nfe = etree.Element("NFe", nsmap=nsmap)
        inf_nfe = etree.SubElement(nfe, "infNFe", Id=chave_id, versao="4.00")

        emit = etree.SubElement(inf_nfe, "emit")
        etree.SubElement(emit, "CRT").text = str(crt_emitente)

        det = etree.SubElement(inf_nfe, "det", nItem="1")
        imposto = etree.SubElement(det, "imposto")
        ibscbs = etree.SubElement(imposto, "IBSCBS")
        etree.SubElement(ibscbs, "CST").text = cst
        etree.SubElement(ibscbs, "cClassTrib").text = c_class_trib

        g_ibscbs = etree.SubElement(ibscbs, "gIBSCBS")
        etree.SubElement(g_ibscbs, "vBC").text = f"{v_bc:.2f}"

        # Subgrupo IBS Estadual (gIBSUF)
        g_ibs_uf = etree.SubElement(g_ibscbs, "gIBSUF")
        etree.SubElement(g_ibs_uf, "pIBSUF").text = f"{p_ibs_uf:.4f}"
        if p_red > Decimal("0.00"):
            g_red_uf = etree.SubElement(g_ibs_uf, "gRed")
            etree.SubElement(g_red_uf, "pRedAliq").text = f"{p_red:.2f}"
            etree.SubElement(g_red_uf, "pAliqEfet").text = f"{p_efet_uf:.4f}"
        etree.SubElement(g_ibs_uf, "vIBSUF").text = f"{v_ibs_uf:.2f}"

        # Subgrupo IBS Municipal (gIBSMun)
        g_ibs_mun = etree.SubElement(g_ibscbs, "gIBSMun")
        etree.SubElement(g_ibs_mun, "pIBSMun").text = f"{p_ibs_mun:.4f}"
        if p_red > Decimal("0.00"):
            g_red_mun = etree.SubElement(g_ibs_mun, "gRed")
            etree.SubElement(g_red_mun, "pRedAliq").text = f"{p_red:.2f}"
            etree.SubElement(g_red_mun, "pAliqEfet").text = f"{p_efet_mun:.4f}"
        etree.SubElement(g_ibs_mun, "vIBSMun").text = f"{v_ibs_mun:.2f}"

        # Subgrupo CBS Federal (gCBS)
        g_cbs = etree.SubElement(g_ibscbs, "gCBS")
        etree.SubElement(g_cbs, "pCBS").text = f"{p_cbs:.4f}"
        if p_red > Decimal("0.00"):
            g_red_cbs = etree.SubElement(g_cbs, "gRed")
            etree.SubElement(g_red_cbs, "pRedAliq").text = f"{p_red:.2f}"
            etree.SubElement(g_red_cbs, "pAliqEfet").text = f"{p_efet_cbs:.4f}"
        etree.SubElement(g_cbs, "vCBS").text = f"{v_cbs:.2f}"

        # Grupo Totalizador obrigatório (IBSCBSTot - evita Rejeição 349)
        total = etree.SubElement(inf_nfe, "total")
        tot_ibscbs = etree.SubElement(total, "IBSCBSTot")
        etree.SubElement(tot_ibscbs, "vBCIBSCBS").text = f"{v_bc:.2f}"

        tot_g_ibs = etree.SubElement(tot_ibscbs, "gIBS")
        tot_uf = etree.SubElement(tot_g_ibs, "gIBSUF")
        etree.SubElement(tot_uf, "vIBSUF").text = f"{v_ibs_uf:.2f}"
        tot_mun = etree.SubElement(tot_g_ibs, "gIBSMun")
        etree.SubElement(tot_mun, "vIBSMun").text = f"{v_ibs_mun:.2f}"
        etree.SubElement(tot_g_ibs, "vIBS").text = f"{v_ibs_tot:.2f}"

        tot_g_cbs = etree.SubElement(tot_ibscbs, "gCBS")
        etree.SubElement(tot_g_cbs, "vCBS").text = f"{v_cbs:.2f}"

        xml_str = etree.tostring(nfe, encoding="utf-8", xml_declaration=True, pretty_print=True).decode("utf-8")

    else:
        # Layout DPS da NFS-e Nacional (NT 004 / NT 007 SEFIN Nacional)
        nsmap = {None: NFSE_NS}
        dps = etree.Element("DPS", nsmap=nsmap, versao="1.01")
        inf_dps = etree.SubElement(dps, "infDPS", Id=chave_id)
        valores = etree.SubElement(inf_dps, "valores")
        trib = etree.SubElement(valores, "trib")
        ibscbs = etree.SubElement(trib, "IBSCBS")
        etree.SubElement(ibscbs, "CST").text = cst
        etree.SubElement(ibscbs, "cClassTrib").text = c_class_trib
        etree.SubElement(ibscbs, "vBC").text = f"{v_bc:.2f}"
        etree.SubElement(ibscbs, "pIBSUF").text = f"{p_efet_uf:.4f}"
        etree.SubElement(ibscbs, "vIBSUF").text = f"{v_ibs_uf:.2f}"
        etree.SubElement(ibscbs, "pIBSMun").text = f"{p_efet_mun:.4f}"
        etree.SubElement(ibscbs, "vIBSMun").text = f"{v_ibs_mun:.2f}"
        etree.SubElement(ibscbs, "pCBS").text = f"{p_efet_cbs:.4f}"
        etree.SubElement(ibscbs, "vCBS").text = f"{v_cbs:.2f}"

        xml_str = etree.tostring(dps, encoding="utf-8", xml_declaration=True, pretty_print=True).decode("utf-8")

    return json.dumps({
        "status": "xml_gerado_com_sucesso",
        "resumo_calculo": {
            "vBC": str(v_bc),
            "vIBSUF": str(v_ibs_uf),
            "vIBSMun": str(v_ibs_mun),
            "vIBS_Total": str(v_ibs_tot),
            "vCBS": str(v_cbs)
        },
        "xml": xml_str
    }, ensure_ascii=False)


# =====================================================================
# TOOL 2: VALIDADOR DE REGRAS DE NEGÓCIO E SCHEMA (NT 2025.002-RTC)
# =====================================================================

def validar_xml_ibscbs_regras_sefaz(xml_conteudo: str, caminho_xsd_opcional: str = "") -> str:
    """
    Audita um arquivo XML de NF-e ou DPS/NFS-e verificando as Regras de Validação (RV)
    do IBS e da CBS da Receita Federal / SEFAZ em 2026 (Rejeições 349, 1022, 1026, 1037, 1041, 1115).
    """
    erros_rejeicao = []
    alertas_fiscais = []

    try:
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_conteudo.encode("utf-8"), parser=parser)
    except Exception as e:
        return json.dumps({"valido": False, "erro_sintaxe_xml": str(e)}, ensure_ascii=False)

    # 1. Validação opcional contra arquivo XSD oficial se fornecido
    if caminho_xsd_opcional and os.path.exists(caminho_xsd_opcional):
        try:
            xmlschema_doc = etree.parse(caminho_xsd_opcional)
            xmlschema = etree.XMLSchema(xmlschema_doc)
            if not xmlschema.validate(root):
                for erro in xmlschema.error_log:
                    erros_rejeicao.append(f"Rejeição E1235 / Falha de Schema XSD (Linha {erro.line}): {erro.message}")
        except Exception as e:
            alertas_fiscais.append(f"Não foi possível processar o XSD informado: {e}")

    # Remove namespaces para simplificar a inspeção XPath agnóstica
    for elem in root.getiterator():
        if not hasattr(elem.tag, "find"):
            continue
        i = elem.tag.find("}")
        if i >= 0:
            elem.tag = elem.tag[i + 1:]

    grupos_item_ibscbs = root.xpath("//det/imposto/IBSCBS | //infDPS/valores/trib/IBSCBS")
    if not grupos_item_ibscbs:
        erros_rejeicao.append("Rejeição 1115: Grupo IBSCBS não informado nos itens do documento fiscal.")
        return json.dumps({"valido": False, "rejeicoes_sefaz": erros_rejeicao}, ensure_ascii=False)

    soma_vbc = Decimal("0.00")
    soma_vibs_uf = Decimal("0.00")
    soma_vibs_mun = Decimal("0.00")
    soma_vcbs = Decimal("0.00")

    for idx, ibscbs in enumerate(grupos_item_ibscbs, start=1):
        cst = ibscbs.findtext("CST", default="").strip()
        c_class = ibscbs.findtext("cClassTrib", default="").strip()

        # Validação CST x cClassTrib (Rejeição 1022 / 1023)
        if cst not in TABELA_CCLASS_VALIDOS:
            erros_rejeicao.append(f"Item {idx} - Rejeição 1022: CST '{cst}' inválido para o IBS/CBS.")
        elif len(c_class) != 6 or not c_class.startswith(cst):
            erros_rejeicao.append(
                f"Item {idx} - Rejeição 1023: cClassTrib '{c_class}' incompatível com o CST '{cst}' "
                "(os 3 primeiros dígitos do cClassTrib devem corresponder ao CST)."
            )

        # Extração para layout NF-e (gIBSCBS)
        g_ibscbs = ibscbs.find("gIBSCBS")
        if g_ibscbs is not None:
            v_bc = _d(g_ibscbs.findtext("vBC", default="0.00"))
            p_ibs_uf = _d(g_ibscbs.findtext("gIBSUF/pIBSUF", default="0.0000"), "0.0001")
            v_ibs_uf = _d(g_ibscbs.findtext("gIBSUF/vIBSUF", default="0.00"))
            p_ibs_mun = _d(g_ibscbs.findtext("gIBSMun/pIBSMun", default="0.0000"), "0.0001")
            v_ibs_mun = _d(g_ibscbs.findtext("gIBSMun/vIBSMun", default="0.00"))
            p_cbs = _d(g_ibscbs.findtext("gCBS/pCBS", default="0.0000"), "0.0001")
            v_cbs = _d(g_ibscbs.findtext("gCBS/vCBS", default="0.00"))

            p_efet_uf = _d(g_ibscbs.findtext("gIBSUF/gRed/pAliqEfet", default=str(p_ibs_uf)), "0.0001")
            p_efet_mun = _d(g_ibscbs.findtext("gIBSMun/gRed/pAliqEfet", default=str(p_ibs_mun)), "0.0001")
            p_efet_cbs = _d(g_ibscbs.findtext("gCBS/gRed/pAliqEfet", default=str(p_cbs)), "0.0001")

            # Validação de Alíquotas 2026 (Rejeição 1026 e 1037)
            if (p_ibs_uf + p_ibs_mun) != Decimal("0.1000") and cst in ("000", "200"):
                erros_rejeicao.append(
                    f"Item {idx} - Rejeição 1026: Soma das alíquotas nominais de IBS ({p_ibs_uf + p_ibs_mun}%) "
                    "difere da alíquota-teste de 0,1000% vigente em 2026."
                )
            if p_cbs != Decimal("0.9000") and cst in ("000", "200"):
                erros_rejeicao.append(
                    f"Item {idx} - Rejeição 1037: Alíquota nominal da CBS ({p_cbs}%) "
                    "difere da alíquota-teste de 0,9000% vigente em 2026."
                )

            # Validação do cálculo aritmético (Rejeição 1041 - tolerância de R$ 0,01)
            esperado_uf = _d(v_bc * (p_efet_uf / Decimal("100")))
            if abs(v_ibs_uf - esperado_uf) > Decimal("0.01"):
                erros_rejeicao.append(
                    f"Item {idx} - Rejeição 1041: Valor do IBS da UF ({v_ibs_uf}) difere do calculado ({esperado_uf})."
                )

            esperado_cbs = _d(v_bc * (p_efet_cbs / Decimal("100")))
            if abs(v_cbs - esperado_cbs) > Decimal("0.01"):
                erros_rejeicao.append(
                    f"Item {idx} - Rejeição 1043: Valor da CBS ({v_cbs}) difere do calculado ({esperado_cbs})."
                )

            soma_vbc += v_bc
            soma_vibs_uf += v_ibs_uf
            soma_vibs_mun += v_ibs_mun
            soma_vcbs += v_cbs

    # Validação do grupo Totalizador IBSCBSTot em NF-e (Rejeição 349)
    if root.tag == "NFe" or root.xpath("//infNFe"):
        tot = root.find(".//total/IBSCBSTot")
        if tot is None:
            erros_rejeicao.append(
                "Rejeição 349: Total de IBS/CBS (tag IBSCBSTot) deve ser informado se existir IBSCBS nos itens."
            )
        else:
            tot_vbc = _d(tot.findtext("vBCIBSCBS", default="0.00"))
            tot_vibs = _d(tot.findtext("gIBS/vIBS", default="0.00"))
            tot_vcbs = _d(tot.findtext("gCBS/vCBS", default="0.00"))

            if tot_vbc != soma_vbc:
                erros_rejeicao.append(
                    f"Rejeição 349 (Base): vBCIBSCBS no total ({tot_vbc}) difere da soma dos itens ({soma_vbc})."
                )
            if tot_vibs != (soma_vibs_uf + soma_vibs_mun):
                erros_rejeicao.append(
                    f"Rejeição 349 (IBS): vIBS no total ({tot_vibs}) difere da soma UF+Mun ({soma_vibs_uf + soma_vibs_mun})."
                )
            if tot_vcbs != soma_vcbs:
                erros_rejeicao.append(
                    f"Rejeição 349 (CBS): vCBS no total ({tot_vcbs}) difere da soma dos itens ({soma_vcbs})."
                )

    return json.dumps({
        "valido": len(erros_rejeicao) == 0,
        "quantidade_rejeicoes": len(erros_rejeicao),
        "rejeicoes_sefaz": erros_rejeicao,
        "alertas": alertas_fiscais
    }, ensure_ascii=False)


# =====================================================================
# TOOL 3: ASSINADOR DIGITAL XMLDSig ICP-BRASIL (C14N ENVELOPED)
# =====================================================================

def assinar_xml_icp_brasil(
    xml_conteudo: str,
    uri_id_alvo: str,
    algoritmo_hash: str = "sha256"
) -> str:
    """
    Assina digitalmente o XML da NF-e (infNFe) ou DPS/NFS-e Nacional (infDPS) no padrão
    XMLDSig Enveloped com canonização C14N exigido pela Receita Federal e SEFIN Nacional.

    Args:
        xml_conteudo: String contendo o XML a ser assinado.
        uri_id_alvo: Valor do atributo Id da tag a assinar (ex: 'NFe352610...' ou 'DPS3552205...').
        algoritmo_hash: 'sha1' (padrão SEFAZ NF-e 4.00) ou 'sha256' (SEFIN Nacional / DPS).
    """
    pfx_path = os.getenv("ECNPJ_PFX_PATH", "./certificados/empresa_ecnpj.pfx")
    pfx_pass = os.getenv("ECNPJ_PFX_PASSWORD", "")

    if not os.path.exists(pfx_path):
        return json.dumps({
            "status": "certificado_ausente",
            "instrucao": f"Arquivo .pfx não localizado em '{pfx_path}'. Defina a variável ECNPJ_PFX_PATH."
        }, ensure_ascii=False)

    try:
        with open(pfx_path, "rb") as f:
            pfx_bytes = f.read()

        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_bytes,
            pfx_pass.encode("utf-8") if pfx_pass else None
        )

        cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_conteudo.encode("utf-8"), parser=parser)

        sig_alg = "rsa-sha1" if algoritmo_hash.lower() == "sha1" else "rsa-sha256"
        dig_alg = "sha1" if algoritmo_hash.lower() == "sha1" else "sha256"

        signer = XMLSigner(
            method=methods.enveloped,
            signature_algorithm=sig_alg,
            digest_algorithm=dig_alg,
            c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
        )
        # Remove o prefixo 'ds:' para atender ao schema XSD xmldsig-core-schema_v1.01.xsd da SEFAZ
        signer.namespaces = {None: "http://www.w3.org/2000/09/xmldsig#"}

        xml_assinado = signer.sign(
            root,
            key=key_pem,
            cert=cert_pem,
            reference_uri=f"#{uri_id_alvo}"
        )

        xml_final = etree.tostring(xml_assinado, encoding="utf-8", xml_declaration=True).decode("utf-8")
        return json.dumps({
            "status": "xml_assinado_com_sucesso",
            "uri_assinada": f"#{uri_id_alvo}",
            "padrao_c14n": "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
            "xml_assinado": xml_final
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "erro_assinatura_xmldsig", "detalhe": str(e)}, ensure_ascii=False)


# =====================================================================
# EXECUÇÃO COM O AGENTE GEMINI 4 ARGON
# =====================================================================

if __name__ == "__main__":
    client = genai.Client()
    agente_xml = client.chats.create(
        model="gemini-4-argon",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Você é um Agente Auditor de Documentos Fiscais Eletrônicos (DF-e) da Reforma Tributária. "
                "Sempre que solicitado, gere o XML com as tags IBSCBS e IBSCBSTot, execute o validador "
                "das Regras de Validação da SEFAZ (NT 2025.002-RTC) e assine quando houver certificado configurado."
            ),
            tools=[
                gerar_xml_ibscbs_dfe,
                validar_xml_ibscbs_regras_sefaz,
                assinar_xml_icp_brasil
            ],
            temperature=0.1
        )
    )

    prompt = (
        "Gere o XML de uma NF-e (chave Id 'NFe35261012345678000199550010000001001000001000') "
        "para um serviço/produto médico com redução de 60% na alíquota (CST 200, cClassTrib 200060) "
        "no valor base de R$ 2.500,00 em 2026 (IBS UF 0,05%, IBS Mun 0,05%, CBS 0,90%). "
        "Em seguida, valide o XML gerado nas regras da SEFAZ para confirmar que não há Rejeição 349 ou 1041."
    )

    resposta = agente_xml.send_message(prompt)
    print(resposta.text)