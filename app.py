import streamlit as st
import os
from openai import AzureOpenAI
import gspread
from google.oauth2.service_account import Credentials

# =============================================================================
# 1. 画面の初期設定
# =============================================================================
st.set_page_config(page_title="AI English QA Test", layout="centered")

st.title("🤖 AI English Performance Test")
st.write("画面の指示に従って、英語の質問に口頭で答えてください。")

# =============================================================================
# 2. Secretsの徹底チェック (エラーを画面に日本語で出すための処理)
# =============================================================================
try:
    # Azure OpenAI 設定の取得
    AZURE_API_KEY = st.secrets["AZURE_OPENAI_API_KEY"]
    AZURE_ENDPOINT = st.secrets["AZURE_OPENAI_ENDPOINT"]
    AZURE_VERSION = st.secrets["AZURE_OPENAI_API_VERSION"]
    
    DEPLOY_CHAT = st.secrets["AZURE_DEPLOYMENT_CHAT"]
    DEPLOY_WHISPER = st.secrets["AZURE_DEPLOYMENT_WHISPER"]
    DEPLOY_TTS = st.secrets["AZURE_DEPLOYMENT_TTS"]
    
    # Google スプレッドシート設定の有無を個別チェック
    if "connections" not in st.secrets or "gsheets" not in st.secrets["connections"]:
        st.error("❌ Secretsの中に `[connections.gsheets]` というグループ名が見つかりません。設定ファイルの一番下に正しく書かれているか確認してください。")
        st.stop()
        
    gsheets_secrets = st.secrets["connections"]["gsheets"]
    
    # 必須項目のチェックリスト
    required_keys = ["spreadsheet", "type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "client_x509_cert_url"]
    missing_keys = [k for k in required_keys if k not in gsheets_secrets]
    
    if missing_keys:
        st.error(f"❌ Secretsの `[connections.gsheets]` の中に、以下の項目が足りないか、スペルが間違っています：\n\n**{missing_keys}**")
        st.stop()
        
    # URLの取得
    SPREADSHEET_URL = gsheets_secrets["spreadsheet"]
    
    # サービスアカウント辞書を安全に組み立て
    creds_dict = {
        "type": gsheets_secrets["type"],
        "project_id": gsheets_secrets["project_id"],
        "private_key_id": gsheets_secrets["private_key_id"],
        "private_key": gsheets_secrets["private_key"],
        "client_email": gsheets_secrets["client_email"],
        "client_id": gsheets_secrets["client_id"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": gsheets_secrets["client_x509_cert_url"],
        "universe_domain": "googleapis.com"
    }
except KeyError as ke:
    st.error(f"❌ AzureまたはGoogleの設定項目が見つかりません。名前が間違っている可能性があります: **{ke}**")
    st.stop()
except Exception as e:
    st.error(f"❌ Secretsの読み込み段階で予期せぬエラーが発生しました: {e}")
    st.stop()

# =============================================================================
# 3. 各種クライアントの初期化 & 接続テスト
# =============================================================================
# Azure OpenAI クライアント
ai_client = AzureOpenAI(
    api_key=AZURE_API_KEY,
    api_version=AZURE_VERSION,
    azure_endpoint=AZURE_ENDPOINT
)

# Google スプレッドシート 確実な認証処理
@st.cache_resource(ttl=3600)
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(credentials)

try:
    gc = get_gspread_client()
    workbook = gc.open_by_url(SPREADSHEET_URL)
except Exception as e:
    st.error("❌ Googleスプレッドシートへのログイン・アクセスに失敗しました。以下の原因が考えられます。")
    st.info(f"**【エラーの生データ】**\n`{str(e)}`")
    st.markdown("""
    1. **スプレッドシートの共有設定漏れ**：スプレッドシートの「共有」で、一般的なアクセスを「リンクを知っている全員（編集者）」にするか、サービスアカウントのメールアドレスを追加してください。
    2. **URLの間違い**：Secretsに書いた `spreadsheet` のURLが正しいか確認してください。
    3. **Private Keyの破損**：鍵のコピーに失敗している、または改行コードが壊れている可能性があります。
    """)
    st.stop()

# =============================================================================
# 4. データ読み込み・書き込み関数
# =============================================================================
def load_config_data():
    """Configシートから問題データを取得"""
    try:
        sheet = workbook.worksheet("Config")
        records = sheet.get_all_records()
        return records
    except Exception as e:
        st.error(f"⚠️ スプレッドシートの中に **「Config」** という名前のシート（タブ）が見つかりません。シート名を確認してください。詳細: {e}")
        return []

def save_result_to_sheets(student_name, student_class, question_no, question_text, transcript, score, feedback):
    """Resultシートに結果を保存"""
    try:
        sheet = workbook.worksheet("Result")
        sheet.append_row([student_name, student_class, question_no, question_text, transcript, score, feedback])
    except Exception as e:
        st.error(f"⚠️ **「Result」** という名前のシート（タブ）が見つからないため、保存に失敗しました: {e}")

# =============================================================================
# 5. アプリケーション進行ロジック
# =============================================================================
# アプリの状態管理（セッション）
if "step" not in st.session_state:
    st.session_state.step = "login"
if "current_q" not in st.session_state:
    st.session_state.current_q = 0

# 問題データのロード
questions = load_config_data()

# --- ステップ1: 生徒情報の入力 ---
if st.session_state.step == "login":
    st.subheader("📝 受験者情報を入力してください")
    student_class = st.text_input("クラス (例: 1-A)", key="input_class")
    student_name = st.text_input("氏名 (例: 山田 太郎)", key="input_name")
    
    if st.button("テストを始める"):
        if student_class and student_name:
            st.session_state.student_class = student_class
            st.session_state.student_name = student_name
            st.session_state.step = "test"
            st.rerun()
        else:
            st.warning("クラスと氏名を両方入力してください。")

# --- ステップ2: テスト本番画面 ---
elif st.session_state.step == "test":
    if not questions:
        st.error("テスト問題（Configシート）からデータが読み込めないため、テストを開始できません。")
        st.stop()
        
    q_idx = st.session_state.current_q
    current_question = questions[q_idx]
    
    st.subheader(f"🗣️ Question {q_idx + 1} / {len(questions)}")
    
    # AIの音声質問を生成・再生
    q_text = current_question.get("QuestionText", "Hello, please introduce yourself.")
    
    audio_key = f"audio_q_{q_idx}"
    if audio_key not in st.session_state:
        with st.spinner("AIが質問を準備中..."):
            response = ai_client.audio.speech.create(
                model=DEPLOY_TTS,
                voice="alloy",
                input=q_text
            )
            st.session_state[audio_key] = response.read()
            
    st.audio(st.session_state[audio_key], format="audio/mp3")
    st.caption("上記の再生ボタンを押して、AIの質問を聴いてください。")
    
    # 生徒の音声録音フォーム
    st.write("---")
    st.write("🎙️ **ここに英語で答えてください：**")
    audio_file = st.audio_input("マイクボタンを押して録音を開始し、話し終わったらもう一度押して停止してください。")
    
    if audio_file is not None:
        if st.button("回答を送信して次へ"):
            with st.spinner("AIがあなたの英語を採点中... 10秒ほどお待ちください。"):
                try:
                    # ① Whisperで文字起こし
                    audio_data = audio_file.read()
                    with open("temp_reply.wav", "wb") as f:
                        f.write(audio_data)
                        
                    with open("temp_reply.wav", "rb") as audio_disk:
                        transcript_res = ai_client.audio.transcriptions.create(
                            model=DEPLOY_WHISPER,
                            file=audio_disk,
                        )
                    student_reply_text = transcript_res.text
                    
                    # ② GPTで自動採点
                    prompt = f"""
                    あなたは中学校の親切な英語の先生です。生徒のパフォーマンステストを採点してください。
                    
                    【AIの質問】: "{q_text}"
                    【生徒の回答】: "{student_reply_text}"
                    
                    以下の項目を厳密に評価し、スプレッドシート保存用に結果を出力してください。
                    1. 点数 (10点満点中の数字のみ)
                    2. 生徒への日本語でのアドバイス・褒め言葉（2文程度）
                    
                    出力フォーマットは必ず以下のようにしてください。
                    点数: [数字]
                    フィードバック: [アドバイス内容]
                    """
                    
                    chat_res = ai_client.chat.completions.create(
                        model=DEPLOY_CHAT,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3
                    )
                    gpt_output = chat_res.choices[0].message.content
                    
                    score = "未採点"
                    feedback = gpt_output
                    for line in gpt_output.split("\n"):
                        if "点数:" in line:
                            score = line.replace("点数:", "").strip()
                        if "フィードバック:" in line:
                            feedback = line.replace("フィードバック:", "").strip()
                    
                    # ③ スプレッドシート（Resultシート）に保存
                    save_result_to_sheets(
                        st.session_state.student_class,
                        st.session_state.student_name,
                        q_idx + 1,
                        q_text,
                        student_reply_text,
                        score,
                        feedback
                    )
                    
                    if os.path.exists("temp_reply.wav"):
                        os.remove("temp_reply.wav")
                    
                    # ④ 進行管理
                    if q_idx + 1 < len(questions):
                        st.session_state.current_q += 1
                        st.success("回答を記録しました！次の問題に進みます。")
                        st.rerun()
                    else:
                        st.session_state.step = "finish"
                        st.rerun()
                        
                except Exception as eval_err:
                    st.error(f"採点処理中にエラーが発生しました。もう一度お試しください。詳細: {eval_err}")

# --- ステップ3: テスト終了画面 ---
elif st.session_state.step == "finish":
    st.balloons()
    st.subheader("🎉 お疲れ様でした！")
    st.success(f"{st.session_state.student_name} さんのパフォーマンステストはすべて終了しました。")
    st.write("結果は自動的に先生のスプレッドシートに保存されました。タブレットを閉じて終了してください。")
