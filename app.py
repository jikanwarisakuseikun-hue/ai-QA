import streamlit as st
import pandas as pd
import datetime
import io
import time
import google.generativeai as genai
from gtts import gTTS
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account
import gspread

# --- 1. ページ基本設定 & セッション状態の初期化 ---
st.set_page_config(page_title="AI英語QAテスト", page_icon="🇬🇧", layout="centered")

if "test_started" not in st.session_state:
    st.session_state.test_started = False
if "current_q_idx" not in st.session_state:
    st.session_state.current_q_idx = 0
if "student_info" not in st.session_state:
    st.session_state.student_info = {}
if "answers_cache" not in st.session_state:
    st.session_state.answers_cache = {}
if "start_time" not in st.session_state:
    st.session_state.start_time = None
if "time_records" not in st.session_state:
    st.session_state.time_records = {}
if "current_feedback" not in st.session_state:
    st.session_state.current_feedback = None

# Gemini APIクライアントの初期化
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

# Googleドライブの保存先フォルダID
GOOGLE_DRIVE_FOLDER_ID = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]

# --- 2. スプレッドシート接続の初期化 ---
@st.cache_resource
def get_gspread_client():
    creds_info = st.secrets["connections"]["gsheets"]
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def get_spreadsheet():
    gc = get_gspread_client()
    sheet_url = st.secrets["spreadsheet"]
    return gc.open_by_url(sheet_url)

# --- 3. AI & 音声 連携関数 ---

def generate_ai_voice(text: str):
    """【完全無料】gTTSを使用して英語テキストから高精度な音声を生成"""
    try:
        tts = gTTS(text=text, lang='en', slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception as e:
        st.error(f"AI音声の生成に失敗しました: {e}")
        return None

def analyze_and_evaluate(audio_bytes, question_text: str, criteria: str):
    """【Gemini API】音声からダイレクトに「文字起こし」と「採点」を同時に実行"""
    try:
        prompt = f"""
        あなたは中学校の英語教師です。
        添付された生徒の録音音声（英語）を聴いて、以下の2つのタスクを行ってください。

        【タスク1: 文字起こし】
        生徒が何と言っているか、英語で正確に文字起こししてください。

        【タスク2: 採点・評価】
        先生が提示した質問と評価基準に照らし合わせて、生徒の回答を採点してください。
        質問: {question_text}
        評価基準: {criteria}

        【出力フォーマット】
        必ず以下の形式で出力してください。これ以外の挨拶などは含めないでください。
        
        ■文字起こし:
        (ここに文字起こしした英文)
        
        ■評価結果:
        判定: (A / B / C のいずれか)
        アドバイス: (生徒への優しい日本語のアドバイス)
        """
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([
            {'mime_type': 'audio/wav', 'data': audio_bytes},
            prompt
        ])
        
        result_text = response.text
        student_speech = "[文字起こしの抽出に失敗しました]"
        eval_result = result_text
        
        if "■文字起こし:" in result_text and "■評価結果:" in result_text:
            parts = result_text.split("■評価結果:")
            eval_result = "■評価結果:" + parts[1]
            student_speech = parts[0].replace("■文字起こし:", "").strip()
            
        return student_speech, eval_result
    except Exception as e:
        return f"[分析失敗: {e}]", f"[AI採点失敗: {e}]"

def upload_to_drive(audio_bytes, file_name) -> str:
    """Googleドライブの指定フォルダへ音声をアップロード (共有ドライブ完全対応版)"""
    try:
        creds_info = st.secrets["connections"]["gsheets"]
        scopes = ['https://www.googleapis.com/auth/drive']
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
        drive_service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {'name': file_name, 'parents': [GOOGLE_DRIVE_FOLDER_ID]}
        media = MediaIoBaseUpload(io.BytesIO(audio_bytes), mimetype='audio/wav', resumable=True)
        
        file = drive_service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink',
            supportsAllDrives=True
        ).execute()
        return file.get('webViewLink', '')
    except Exception as e:
        st.error(f"Googleドライブへのアップロードに失敗しました: {e}")
        return "Upload Failed"

# --- 4. スプレッドシート操作関数 ---

def load_all_config():
    """Configシートを全件取得"""
    try:
        sh = get_spreadsheet()
        worksheet = sh.worksheet("Config")
        data = worksheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Configシートの読み込みに失敗しました: {e}")
        return pd.DataFrame()

def save_results_to_sheet(student_info: dict, answers: dict, time_records: dict, num_questions: int):
    """Resultsシートへデータを追記"""
    # 【タイムスタンプJST強制修正】
    # サーバーの環境に依存せず、タイムゾーンに+9時間を指定して東京（日本）の時間を取得します
    t_delta = datetime.timedelta(hours=9)
    JST = datetime.timezone(t_delta, 'JST')
    now_jst = datetime.datetime.now(JST)
    timestamp_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    row_data = [
        str(timestamp_str),                                        # Timestamp (日本時間)
        str(student_info.get("school", "")),                       # School
        str(student_info.get("grade", "")),                        # Grade
        str(student_info.get("class_num", "")),                    # Class
        str(student_info.get("attend_num", "")),                   # Number
        str(student_info.get("name", "")),                         # Name
    ]
    
    for i in range(1, 6):
        if i <= num_questions:
            row_data.append(str(answers.get(f"q{i}_speech", "")))
            row_data.append(str(answers.get(f"q{i}_eval", "")))
            row_data.append(str(answers.get(f"q{i}_audio_url", "")))
            row_data.append(str(time_records.get(i, 0))) 
        else:
            row_data.extend(["", "", "", ""]) 
            
    try:
        sh = get_spreadsheet()
        worksheet = sh.worksheet("Results")
        worksheet.append_row(row_data)
        st.success("テスト結果がスプレッドシートに正常に保存されました。")
    except Exception as e:
        st.error(f"結果の保存中にエラーが発生しました: {e}")

# --- 5. メイン処理（生徒用画面のみ） ---
st.title("🇬🇧 AI English QA Test")

df_config_all = load_all_config()

if not st.session_state.test_started:
    st.subheader("受験者情報を入力してください")
    
    if not df_config_all.empty and 'School' in df_config_all.columns:
        df_config_all = df_config_all.astype(str)
        available_schools = sorted(list(df_config_all['School'].dropna().unique()))
        available_grades = sorted(list(df_config_all['Grade'].dropna().unique()))
        available_classes = sorted(list(df_config_all['Class'].dropna().unique()))
    else:
        available_schools = ["〇〇中学校"]
        available_grades = ["1年", "2年", "3年"]
        available_classes = ["1組", "2組", "3組"]
    
    school = st.selectbox("学校名", available_schools)
    grade = st.selectbox("学年", available_grades)
    class_num = st.selectbox("クラス", available_classes)
    attend_num = st.selectbox("出席番号", [i for i in range(1, 51)], index=0)
    name = st.text_input("氏名（例：タロウ / ニックネームなど個人が特定できないもの）")
    
    if st.button("テストを始める", type="primary"):
        if name.strip() == "":
            st.warning("受験者の氏名・ニックネームを入力してください。")
        else:
            if df_config_all.empty:
                st.error("スプレッドシートのConfigシートからデータを読み込めませんでした。シートの設定を確認してください。")
            else:
                df_config_all = df_config_all.astype(str)
                student_config = df_config_all[
                    (df_config_all['School'] == str(school)) & 
                    (df_config_all['Grade'] == str(grade)) & 
                    (df_config_all['Class'] == str(class_num))
                ]
                
                if student_config.empty:
                    st.error("選択したクラスの設定がスプレッドシート内に見つかりません。シートを確認してください。")
                else:
                    st.session_state.student_info = {
                        "school": str(school), 
                        "grade": str(grade), 
                        "class_num": str(class_num),
                        "attend_num": str(attend_num), 
                        "name": str(name.strip()),
                        "config": student_config.iloc[0].to_dict()
                    }
                    st.session_state.test_started = True
                    st.session_state.current_q_idx = 1
                    st.session_state.answers_cache = {}
                    st.session_state.time_records = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    st.session_state.start_time = None
                    st.session_state.current_feedback = None
                    st.rerun()

else:
    student_config = st.session_state.student_info["config"]
    
    try:
        num_questions = int(float(student_config.get("num_questions", 3)))
    except:
        num_questions = 3
        
    idx = st.session_state.current_q_idx
    
    if idx <= num_questions:
        st.markdown(f"### 🚀 Question {idx} / {num_questions}")
        q_text = student_config.get(f"q{idx}_text", "")
        q_criteria = student_config.get(f"q{idx}_criteria", "")
        
        if st.session_state.start_time is None:
            st.session_state.start_time = time.time()
            
        voice_key = f"ai_voice_{idx}"
        if voice_key not in st.session_state:
            with st.spinner("AIが質問音声を生成しています... 🎧"):
                st.session_state[voice_key] = generate_ai_voice(q_text)
        
        st.markdown("#### 🎧 1. AIの質問を聴いてください")
        if st.session_state[voice_key]:
            st.audio(st.session_state[voice_key], format="audio/mp3")
        
        st.markdown("---")
        st.markdown("#### 🗣️ 2. あなたの回答を録音してください")
        
        if st.session_state.current_feedback is None:
            audio_file = st.audio_input("ここを押して発話・録音", key=f"audio_{idx}")
            
            if st.button("回答を送信する", type="primary", key=f"submit_{idx}"):
                if audio_file is None:
                    st.warning("音声が録音されていません。")
                else:
                    final_elapsed = int(time.time() - st.session_state.start_time)
                    st.session_state.time_records[idx] = final_elapsed
                    
                    with st.spinner("AIがあなたの英語を分析中... 📝"):
                        audio_bytes = audio_file.read()
                        info = st.session_state.student_info
                        
                        file_name = f"{info['grade']}{info['class_num']}_{info['attend_num']}番_{info['name']}_Q{idx}.wav"
                        audio_url = upload_to_drive(audio_bytes, file_name)
                        
                        student_speech, eval_result = analyze_and_evaluate(audio_bytes, q_text, q_criteria)
                        
                        st.session_state.answers_cache[f"q{idx}_speech"] = str(student_speech)
                        st.session_state.answers_cache[f"q{idx}_eval"] = str(eval_result)
                        st.session_state.answers_cache[f"q{idx}_audio_url"] = str(audio_url)
                        
                        st.session_state.current_feedback = {
                            "speech": student_speech,
                            "eval": eval_result
                        }
                    st.rerun()
        
        if st.session_state.current_feedback is not None:
            st.success("🎯 回答の送信が完了しました！AI教師からのフィードバックです。")
            
            with st.container(border=True):
                st.markdown("#### 🗣️ あなたが話した英語（AIの文字起こし）")
                st.code(st.session_state.current_feedback["speech"], language="text")
                
                st.markdown("#### 📝 採点・アドバイス")
                st.info(st.session_state.current_feedback["eval"])
                
                st.write(f"⏱️ かかった時間: **{st.session_state.time_records[idx]}秒**")
            
            st.markdown("確認したら、下のボタンを押して次の問題に進んでください。")
            if st.button("次の質問へ進む ➡️", type="primary", key=f"next_btn_{idx}"):
                st.session_state.current_feedback = None
                st.session_state.start_time = None 
                st.session_state.current_q_idx += 1
                st.rerun()
                
    else:
        st.balloons()
        st.success("🎉 すべての質問が終了しました！お疲れ様でした。")
        st.write("データを先生に送信しています。画面を閉じずにそのままお待ちください...")
        
        if "data_saved" not in st.session_state:
            with st.spinner("保存中..."):
                save_results_to_sheet(
                    st.session_state.student_info,
                    st.session_state.answers_cache,
                    st.session_state.time_records,
                    num_questions
                )
            st.session_state.data_saved = True
            
        st.markdown("---")
        if st.button("最初の画面に戻る（次の生徒の受験用）"):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

# --- 6. 著作権表示（フッター） ---
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888888; font-size: 0.8em;'>"
    "© 2026 Shogo Takeuchi. All Rights Reserved."
    "</div>",
    unsafe_allow_html=True
)
