import streamlit as st
import pandas as pd
import datetime
import io
import time
import os
import random
import google.generativeai as genai  # Gemini用
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
# 考える時間（シンキングタイム）のタイマー制御用
if "timer_done" not in st.session_state:
    st.session_state.timer_done = False
if "last_timer_q_idx" not in st.session_state:
    st.session_state.last_timer_q_idx = 0

# Gemini APIの初期化
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
except Exception as e:
    st.error(f"⚠️ Secretsの「GEMINI_API_KEY」の読み込みに失敗しました: {e}")

try:
    GOOGLE_DRIVE_FOLDER_ID = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]
except Exception as e:
    st.error(f"⚠️ Secretsの「GOOGLE_DRIVE_FOLDER_ID」の読み込みに失敗しました: {e}")

# --- 2. スプレッドシート接続の初期化 ---
@st.cache_resource
def get_gspread_client():
    try:
        creds_info = st.secrets["connections"]["gsheets"]
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"⚠️ Googleサービスアカウントの認証に失敗しました。Secretsの設定を確認してください: {e}")
        st.stop()

def get_spreadsheet():
    try:
        gc = get_gspread_client()
        spreadsheet_url = st.secrets["spreadsheet"]
        return gc.open_by_url(spreadsheet_url)
    except Exception as e:
        st.error(f"⚠️ スプレッドシートのオープンに失敗しました。URLまたはアクセス権限を確認してください: {e}")
        st.stop()

# --- 3. AI & 音声 連携関数 (リトライ機能搭載) ---

def generate_ai_voice(text: str):
    try:
        tts = gTTS(text=text, lang='en', slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception as e:
        st.error(f"AI音声の生成に失敗しました: {e}")
        return None

def analyze_and_evaluate_gemini_with_retry(audio_bytes, question_text: str, criteria: str, max_retries=5):
    """【堅牢版】同時アクセスエラー（429等）を自動リトライで回避する評価関数"""
    
    prompt_evaluation = f"""
    あなたは中学校の英語教師です。
    提示した質問・評価基準と、添付された生徒の録音音声（英語）を照らし合わせて、以下の2つのタスクを行ってください。

    【先生が提示した質問】: {question_text}
    【先生が提示した評価基準】: {criteria}

    【タスク1: 文字起こし】
    生徒が何と言っているか、英語で正確に文字起こししてください。無音や英語として聞き取れない場合は 「No speech」 と出力してください。

    【タスク2: 採点・評価】
    評価基準に沿って、判定（A/B/Cのいずれか）と生徒への優しい日本語アドバイスを作成してください。

    【出力フォーマット】
    必ず以下のフォーマットを厳守して出力してください。これ以外の挨拶や解説は含めないでください。
    ■文字起こし:
    (ここに文字起こしした英文)
    ■評価結果:
    判定: (A / B / C のいずれか)
    アドバイス: (生徒への優しい日本語のアドバイス)
    """

    audio_data = {
        "mime_type": "audio/wav",
        "data": io.BytesIO(audio_bytes).getvalue()
    }

    models_to_try = ["gemini-2.5-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash-lite"]
    
    for attempt in range(max_retries):
        last_error = ""
        for model_name in models_to_try:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content([audio_data, prompt_evaluation])
                result_text = response.text
                
                student_speech = "[文字起こしの抽出に失敗しました]"
                eval_result = result_text
                
                if "■文字起こし:" in result_text and "■評価結果:" in result_text:
                    parts = result_text.split("■評価結果:")
                    eval_result = "■評価結果:" + parts[1]
                    student_speech = parts[0].replace("■文字起こし:", "").strip()
                    
                return student_speech, eval_result, f"🟢 Gemini ({model_name} で解析完了)"
                
            except Exception as e:
                last_error = str(e)
                if "429" in last_error or "Quota" in last_error or "limit" in last_error:
                    time.sleep(1 + random.random())
                continue
        
        if attempt < max_retries - 1:
            wait_time = (2 ** attempt) + random.uniform(0.5, 1.5)
            time.sleep(wait_time)
        else:
            break
            
    return (
        "[エラー] 混雑のためAIが応答しませんでした。", 
        f"Geminiエラー: {last_error}\n時間を置いて再度送信をお試しください。", 
        "🔴 解析失敗（制限オーバー）"
    )

def upload_to_drive_with_retry(audio_bytes, file_name, max_retries=5) -> str:
    """【堅牢版】同時書き込み制限による403/503エラーをリトライで回避するアップロード関数"""
    for attempt in range(max_retries):
        try:
            creds_info = st.secrets["connections"]["gsheets"]
            scopes = ['https://www.googleapis.com/auth/drive']
            creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
            drive_service = build('drive', 'v3', credentials=creds)
            
            file_metadata = {'name': file_name, 'parents': [GOOGLE_DRIVE_FOLDER_ID]}
            media = MediaIoBaseUpload(io.BytesIO(audio_bytes), mimetype='audio/wav', resumable=True)
            
            file = drive_service.files().create(
                body=file_metadata, media_body=media, fields='id, webViewLink', supportsAllDrives=True
            ).execute()
            return file.get('webViewLink', '')
            
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1.5 + random.random() * 1.5)
            else:
                return f"Upload Failed: {e}"

# --- 4. スプレッドシート操作関数 ---
@st.cache_data(ttl=60)
def load_all_config():
    try:
        sh = get_spreadsheet()
        data = sh.worksheet("Config").get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"⚠️ スプレッドシート「Config」シートのデータ取得に失敗しました。詳細エラー: {e}")
        return pd.DataFrame()

def save_results_to_sheet_with_retry(student_info: dict, answers: dict, time_records: dict, num_questions: int, max_retries=5):
    """【堅牢版】同時書き込みによるシート競合をリトライで解決する保存関数"""
    t_delta = datetime.timedelta(hours=9)
    JST = datetime.timezone(t_delta, 'JST')
    row_data = [
        datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        str(student_info.get("school", "")),
        str(student_info.get("grade", "")),
        str(student_info.get("class_num", "")),
        str(student_info.get("attend_num", "")),
        str(student_info.get("name", "")),
    ]
    for i in range(1, 6):
        if i <= num_questions:
            row_data.append(str(answers.get(f"q{i}_speech", "")))
            row_data.append(str(answers.get(f"q{i}_eval", "")))
            row_data.append(str(answers.get(f"q{i}_audio_url", "")))
            row_data.append(str(time_records.get(i, 0))) 
        else:
            row_data.extend(["", "", "", ""]) 
            
    for attempt in range(max_retries):
        try:
            sh = get_spreadsheet()
            sh.worksheet("Results").append_row(row_data)
            st.success("結果がスプレッドシートに保存されました。")
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 + random.random() * 2)
            else:
                st.error(f"保存エラー: {e}。お手数ですがこの画面をスクリーンショット等で保存し、先生に伝えてください。")
                return False

# --- 5. メイン処理 ---
st.title("🇬🇧 AI English QA Test")

df_config_all = load_all_config()

if not st.session_state.test_started:
    st.subheader("受験者情報を入力してください")
    
    if not df_config_all.empty and 'School' in df_config_all.columns:
        df_config_all = df_config_all.astype(str)
        try:
            available_schools = sorted(list(df_config_all['School'].dropna().unique()))
            available_grades = sorted(list(df_config_all['Grade'].dropna().unique()))
            available_classes = sorted(list(df_config_all['Class'].dropna().unique()))
        except Exception as e:
            st.error(f"⚠️ 列名（School, Grade, Class）が見つかりません: {e}")
            available_schools, available_grades, available_classes = ["〇〇中"], ["1年"], ["1組"]
    else:
        st.warning("⚠️ スプレッドシートからデータを取得できませんでした。デフォルトの設定で表示しています。")
        available_schools, available_grades, available_classes = ["〇〇中"], ["1年"], ["1組"]
    
    school = st.selectbox("学校名", available_schools)
    grade = st.selectbox("学年", available_grades)
    class_num = st.selectbox("クラス", available_classes)
    attend_num = st.selectbox("出席番号", [i for i in range(1, 51)], index=0)
    name = st.text_input("氏名（例：タロウ / ニックネーム）")
    
    if st.button("🔄 スプレッドシートからデータを再読込する"):
        st.cache_data.clear()
        st.rerun()
    
    if st.button("テストを始める", type="primary"):
        if name.strip() == "" or df_config_all.empty:
            st.warning("入力内容を確認するか、Configシートを修正してください。")
        else:
            df_config_all = df_config_all.astype(str)
            student_config = df_config_all[(df_config_all['School'] == str(school)) & (df_config_all['Grade'] == str(grade)) & (df_config_all['Class'] == str(class_num))]
            if student_config.empty:
                st.error("入力されたクラス設定がConfigシートに見つかりません。")
            else:
                st.session_state.student_info = {"school": school, "grade": grade, "class_num": class_num, "attend_num": attend_num, "name": name.strip(), "config": student_config.iloc[0].to_dict()}
                st.session_state.test_started = True
                st.session_state.current_q_idx = 1
                st.session_state.answers_cache = {}
                st.session_state.time_records = {1:0, 2:0, 3:0, 4:0, 5:0}
                st.session_state.current_feedback = None
                st.session_state.timer_done = False
                st.session_state.last_timer_q_idx = 0
                st.rerun()
else:
    student_config = st.session_state.student_info["config"]
    num_questions = int(float(student_config.get("num_questions", 3)))
    idx = st.session_state.current_q_idx
    
    if idx <= num_questions:
        st.markdown(f"### 🚀 Question {idx} / {num_questions}")
        q_text = student_config.get(f"q{idx}_text", "")
        q_criteria = student_config.get(f"q{idx}_criteria", "")
        
        if st.session_state.start_time is None:
            st.session_state.start_time = time.time()
            
        voice_key = f"ai_voice_{idx}"
        if voice_key not in st.session_state:
            st.session_state[voice_key] = generate_ai_voice(q_text)
        
        st.markdown("#### 🎧 1. AIの質問を聴いてください")
        if st.session_state[voice_key]:
            st.audio(st.session_state[voice_key], format="audio/mp3")
        
        st.markdown("---")
        
        # --- ⏳ シンキングタイム・カウントダウン機能 ---
        # 該当設問でまだタイマーを実行していなければ処理を開始
        if st.session_state.last_timer_q_idx != idx:
            st.markdown("#### 🧠 2. 答える英語を考えてください（シンキングタイム）")
            # 20秒の考える時間（学校現場に合わせた秒数。必要に応じて変更してください）
            thinking_seconds = 15
            
            progress_bar = st.progress(0.0)
            status_text = st.empty()
            
            for percent_complete in range(thinking_seconds):
                time.sleep(1)
                progress_bar.progress((percent_complete + 1) / thinking_seconds)
                status_text.write(f"⏳ あと **{thinking_seconds - (percent_complete + 1)}** 秒考えてください...")
            
            status_text.write("✅ 考える時間が終了しました！録音を開始しましょう。")
            st.session_state.timer_done = True
            st.session_state.last_timer_q_idx = idx
            time.sleep(0.5)
            st.rerun()

        # タイマー完了後にのみ表示される「発話・録音エリア」
        if st.session_state.timer_done:
            st.markdown("#### 🗣️ 3. あなたの回答を録音してください")
            
            if st.session_state.current_feedback is None:
                audio_file = st.audio_input("ここを押して発話・録音", key=f"audio_{idx}")
                
                if st.button("回答を送信する", type="primary", key=f"submit_{idx}"):
                    if audio_file is None:
                        st.warning("音声が録音されていません。")
                    else:
                        st.session_state.time_records[idx] = int(time.time() - st.session_state.start_time)
                        with st.spinner("AIがあなたの英語を分析中... 📝 (※混雑時は少し時間がかかる場合があります)"):
                            audio_bytes = audio_file.read()
                            info = st.session_state.student_info
                            file_name = f"{info['grade']}{info['class_num']}_{info['attend_num']}番_{info['name']}_Q{idx}.wav"
                            
                            audio_url = upload_to_drive_with_retry(audio_bytes, file_name)
                            student_speech, eval_result, system_status = analyze_and_evaluate_gemini_with_retry(audio_bytes, q_text, q_criteria)
                            
                            st.session_state.answers_cache[f"q{idx}_speech"] = str(student_speech)
                            st.session_state.answers_cache[f"q{idx}_eval"] = str(eval_result)
                            st.session_state.answers_cache[f"q{idx}_audio_url"] = str(audio_url)
                            
                            st.session_state.current_feedback = {"speech": student_speech, "eval": eval_result, "status": system_status}
                        st.rerun()
            
            if st.session_state.current_feedback is not None:
                st.success("🎯 回答の送信が完了しました！")
                with st.container(border=True):
                    st.markdown("#### 🗣️ あなたが話した英語（AIの文字起こし）")
                    st.code(st.session_state.current_feedback["speech"], language="text")
                    st.markdown("#### 📝 採点・アドバイス")
                    st.info(st.session_state.current_feedback["eval"])
                    st.caption(f"🔧 稼働システム情報: {st.session_state.current_feedback['status']}")
                
                if st.button("次の質問へ進む ➡️", type="primary", key=f"next_btn_{idx}"):
                    st.session_state.current_feedback = None
                    st.session_state.start_time = None 
                    st.session_state.timer_done = False  # 次の問題のためにタイマーをリセット
                    st.session_state.current_q_idx += 1
                    st.rerun()
    else:
        st.balloons()
        st.success("🎉 すべての質問が終了しました！")
        if "data_saved" not in st.session_state:
            with st.spinner("スプレッドシートへ最終データを保存中... ⏳"):
                save_results_to_sheet_with_retry(st.session_state.student_info, st.session_state.answers_cache, st.session_state.time_records, num_questions)
            st.session_state.data_saved = True
        if st.button("最初の画面に戻る（次の生徒用）"):
            for key in list(st.session_state.keys()): del st.session_state[key]
            st.rerun()

st.markdown("---")
st.markdown("<div style='text-align: center; color: #888888; font-size: 0.8em;'>© 2026 Shogo Takeuchi. All Rights Reserved.</div>", unsafe_allow_html=True)
