"""給与支給控除一覧表 PDF → 弥生会計インポート用 TXT（振替伝票）"""
import streamlit as st
import pdfplumber
import csv
import io
import json
import re
import pandas as pd
from google import genai

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

_PAYROLL_PROMPT = """\
この給与支給控除一覧表からデータを抽出し、JSONのみ返せ。説明・コードフェンス不要。

{"締め日":"YYYY/MM/DD","役員報酬":数値,"基本給":数値,"通勤手当":数値,"旅費手当":数値,"社会保険料合計":数値,"所得税":数値,"住民税":数値,"従業員":[{"氏名":"氏名","差引支給額":数値,"種別":"役員か社員"}]}

注意:
- 締め日は「（YYYY年MM月DD日締）」や「YYYY MM DD」形式の締切日（支給日ではない）
- 社会保険料合計＝健康保険料＋介護保険料＋厚生年金保険料＋雇用保険料の合計（従業員負担分）
- 従業員一覧は全員分。役員報酬対象者は種別「役員」、それ以外は「社員」
- 通勤手当・旅費手当がない場合は0
- 2ページ目の総合計欄の数値を優先使用"""


def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def parse_payroll_pdf(content: bytes, api_key: str, model: str) -> dict:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not full_text.strip():
        return {"error": "PDFからテキストを抽出できませんでした"}
    client = genai.Client(api_key=api_key)
    prompt = _PAYROLL_PROMPT + "\n\n給与明細テキスト:\n" + full_text[:10000]
    try:
        resp = client.models.generate_content(model=model, contents=[prompt])
        raw = re.sub(r"```(?:json)?", "", resp.text or "").strip().rstrip("`").strip()
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


def payroll_yayoi_rows(data: dict) -> list:
    """振替伝票形式の弥生インポート用行リストを生成する。

    弥生インポートTXT仕様（重要）:
    - col 2 (伝票番号): ""
    - col 4 (日付): YYYY/MM/DD（和暦不可）
    - col 10/16 (消費税額): "" 空文字必須（0を入れると0円で取り込まれる）
    - col 20 (タイプ): 3（振替伝票）
    - col 23-25: ""
    """
    date_str = data["締め日"]  # YYYY/MM/DD のまま使用
    rows = []

    def prow(code, dr_acct, dr_sub, dr_tax, dr_amt,
             cr_acct, cr_sub, cr_tax, cr_amt, memo):
        return [
            code, "", "", date_str,          # 1-4: 識別・伝票番号・決算・日付
            dr_acct, dr_sub, "", dr_tax, dr_amt, "",  # 5-10: 借方（消費税額は必ず""）
            cr_acct, cr_sub, "", cr_tax, cr_amt, "",  # 11-16: 貸方（消費税額は必ず""）
            memo, "", "", 3,                 # 17-20: 摘要・番号・期日・タイプ(振替伝票=3)
            "", "", "", "", "",              # 21-25: 生成元・仕訳メモ・付箋1・付箋2・調整
        ]

    yakuin = int(data.get("役員報酬", 0) or 0)
    kyuyo  = int(data.get("基本給",   0) or 0)
    tsukin = int(data.get("通勤手当", 0) or 0)
    ryohi  = int(data.get("旅費手当", 0) or 0)

    # 借方: 支給項目
    if yakuin:
        rows.append(prow("2110", "役員報酬", "", "対象外", yakuin,
                         "", "", "対象外", 0, "役員報酬"))
    if kyuyo:
        rows.append(prow("2100", "給料手当", "", "対象外", kyuyo,
                         "", "", "対象外", 0, "給与"))
    if tsukin:
        rows.append(prow("2100", "旅費交通費", "通勤手当", "課対仕入込10%適格", tsukin,
                         "", "", "対象外", 0,
                         "通勤手当（非課税）　※「帳簿のみ保存の特例」適用"))
    if ryohi:
        rows.append(prow("2100", "旅費交通費", "旅費日当", "課対仕入込10%適格", ryohi,
                         "", "", "対象外", 0, "旅費日当"))

    shakai  = int(data.get("社会保険料合計", 0) or 0)
    shotoku = int(data.get("所得税",         0) or 0)
    jumin   = int(data.get("住民税",         0) or 0)

    # 貸方: 控除項目
    if shakai:
        rows.append(prow("2100", "", "", "対象外", 0,
                         "法定福利費", "", "対象外", shakai, "社会保険"))
    if shotoku:
        rows.append(prow("2100", "", "", "対象外", 0,
                         "預り金", "源泉所得税（上期）", "対象外", shotoku, "源泉所得税"))
    if jumin:
        rows.append(prow("2100", "", "", "対象外", 0,
                         "預り金", "住民税", "対象外", jumin, "住民税"))

    # 貸方: 個人別差引支給額（最終行のみ "2101"）
    employees = data.get("従業員", [])
    for i, emp in enumerate(employees):
        code = "2101" if i == len(employees) - 1 else "2100"
        sub = "役員報酬" if str(emp.get("種別", "")).strip() == "役員" else "給与"
        rows.append(prow(code, "", "", "対象外", 0,
                         "未払費用", sub, "対象外", int(emp["差引支給額"]), emp["氏名"]))

    return rows


def _to_yayoi_txt(rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_NONNUMERIC, lineterminator="\r\n")
    for row in rows:
        safe = [
            c.encode("cp932", errors="replace").decode("cp932") if isinstance(c, str) else c
            for c in row
        ]
        writer.writerow(safe)
    return buf.getvalue().encode("cp932", errors="replace")


def output_filename(date_str: str) -> str:
    try:
        y, m, _ = date_str.split("/")
        return f"{y}年{int(m)}月_給与_仕訳.txt"
    except Exception:
        return "給与_仕訳.txt"


def main():
    st.set_page_config(page_title="弥生会計インポート（給与明細）", page_icon="💰", layout="wide")
    st.title("💰 弥生会計インポート（給与明細）")
    st.caption("給与支給控除一覧表 PDF → 弥生会計インポート用 TXT（振替伝票）")

    if "parsed" not in st.session_state:
        st.session_state.parsed = None
    if "output" not in st.session_state:
        st.session_state.output = None

    with st.sidebar:
        st.header("⚙ 設定")
        api_key = st.text_input(
            "Gemini APIキー",
            value=_get_secret("GEMINI_API_KEY"),
            type="password",
            help="GeminiによるPDF解析に使用します",
        )
        gemini_model = st.selectbox("モデル", GEMINI_MODELS, index=0)

    uploaded = st.file_uploader(
        "給与支給控除一覧表 PDF をアップロード",
        type=["pdf"],
        help="弥生給与等で出力した月次の給与支給控除一覧表",
    )

    if uploaded and not api_key:
        st.warning("サイドバーに Gemini APIキー を入力してください")

    if uploaded and api_key:
        if st.button("▶ PDF解析", type="primary"):
            st.session_state.parsed = None
            st.session_state.output = None
            with st.spinner("Gemini で解析中..."):
                result = parse_payroll_pdf(uploaded.read(), api_key, gemini_model)
            if "error" in result:
                st.error(f"解析エラー: {result['error']}")
            else:
                st.session_state.parsed = result

    if st.session_state.parsed:
        data = st.session_state.parsed
        st.divider()
        st.subheader("📋 解析結果（編集可）")
        st.caption("Geminiの解析結果を確認し、必要に応じて修正してください。")

        date_in = st.text_input("締め日（YYYY/MM/DD）", value=data.get("締め日", ""))

        st.markdown("**支給項目**")
        c1, c2, c3, c4 = st.columns(4)
        yakuin = c1.number_input("役員報酬",   value=int(data.get("役員報酬",   0) or 0), min_value=0, step=1)
        kyuyo  = c2.number_input("基本給",     value=int(data.get("基本給",     0) or 0), min_value=0, step=1)
        tsukin = c3.number_input("通勤手当",   value=int(data.get("通勤手当",   0) or 0), min_value=0, step=1)
        ryohi  = c4.number_input("旅費手当",   value=int(data.get("旅費手当",   0) or 0), min_value=0, step=1)

        st.markdown("**控除項目**")
        d1, d2, d3 = st.columns(3)
        shakai  = d1.number_input("社会保険料合計（従業員負担）",
                                   value=int(data.get("社会保険料合計", 0) or 0), min_value=0, step=1)
        shotoku = d2.number_input("所得税", value=int(data.get("所得税", 0) or 0), min_value=0, step=1)
        jumin   = d3.number_input("住民税", value=int(data.get("住民税", 0) or 0), min_value=0, step=1)

        st.markdown("**従業員別 差引支給額**")
        emps = data.get("従業員", [])
        df_init = pd.DataFrame(
            emps if emps else [{"氏名": "", "差引支給額": 0, "種別": "社員"}]
        )
        edited = st.data_editor(
            df_init,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "氏名":       st.column_config.TextColumn("氏名", width="medium"),
                "差引支給額": st.column_config.NumberColumn("差引支給額", min_value=0, format="%d"),
                "種別":       st.column_config.SelectboxColumn("種別", options=["社員", "役員"]),
            },
        )

        emp_list = [
            {"氏名": str(r["氏名"]), "差引支給額": int(r["差引支給額"] or 0), "種別": str(r["種別"])}
            for _, r in edited.iterrows()
            if str(r.get("氏名", "")).strip()
        ]

        dr_total = yakuin + kyuyo + tsukin + ryohi
        cr_total = shakai + shotoku + jumin + sum(e["差引支給額"] for e in emp_list)
        diff = dr_total - cr_total

        st.divider()
        col_l, col_r = st.columns(2)
        col_l.metric("借方合計", f"¥{dr_total:,}")
        col_r.metric("貸方合計", f"¥{cr_total:,}",
                     delta=f"差額 {diff:+,}" if diff else None,
                     delta_color="inverse")

        if diff == 0:
            st.success("✅ 貸借一致 — TXT生成できます")
        else:
            st.warning(f"⚠️ 差額 ¥{diff:,} — 数値を修正してください（貸借が一致しないと生成できません）")

        if st.button("📄 弥生TXT生成", type="primary", disabled=(diff != 0)):
            merged = {
                "締め日":         date_in,
                "役員報酬":       yakuin,
                "基本給":         kyuyo,
                "通勤手当":       tsukin,
                "旅費手当":       ryohi,
                "社会保険料合計": shakai,
                "所得税":         shotoku,
                "住民税":         jumin,
                "従業員":         emp_list,
            }
            rows = payroll_yayoi_rows(merged)
            txt_bytes = _to_yayoi_txt(rows)
            fname = output_filename(date_in)
            st.session_state.output = (fname, txt_bytes, len(rows))

    if st.session_state.output:
        fname, txt_bytes, nrows = st.session_state.output
        st.divider()
        st.download_button(
            label=f"📥 ダウンロード: {fname}",
            data=txt_bytes,
            file_name=fname,
            mime="application/octet-stream",
        )
        st.success(f"出力完了: {fname}  ({nrows} 行)")


if __name__ == "__main__":
    main()
