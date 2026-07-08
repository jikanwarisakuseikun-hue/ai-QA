import streamlit as st
import pandas as pd
import datetime
import io
import time
import os
import google.generativeai as genai  # Gemini用
from groq import Groq                # Groq用
from gtts import gTTS
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account
import gspread

# --- 1. ページ基本設定 & セッション状態の初期化 ---
st.set_page_config(page_title="AI英語QAテスト (二刀流ハイブリッド)", page_icon="🇬🇧", layout="centered")

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

# 両方のAPIを初期化
groq_client = Groq(api_key=st.secrets["GROQ_API_KEY"])
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

GOOGLE_DRIVE_FOLDER_ID = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]

# --- 2. スプレッドシート接続の初期化 ---
@st.cache_resource
def get_gspread_client():
    creds_info = st.secrets["connections"]["gsheets"]
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def get_spreadsheet():
    gc = get_gspread_client()
    return gc.open_by_url(st.secrets["spreadsheet"])

# --- 3. AI & 音声 連携関数 (ハイブリッド仕様) ---

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

def analyze_and_evaluate_hybrid(audio_bytes, question_text: str, criteria: str):
    """【二刀流システム】まずはGroqで試み、ダメなら自動でGeminiに切り替える"""
    
    # 共通のプロンプト
    prompt_evaluation = f"""
    あなたは中学校の英語教師です。
    生徒が答えた英文と、提示した質問・評価基準を照らし合わせて、生徒の回答を採点してください。

    【先生が提示した質問】: {question_text}
    【先生が提示した評価基準】: {criteria}
    """

    # --- ルート①: まずは Groq (Whisper + Llama) で実行 ---
    try:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "student_speech.wav"

        # 文字起こし (Groq Whisper)
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file, model="whisper-large-v3", language="en"
        )
        student_speech = transcription.text.strip()
        if not student_speech:
            student_speech = "[音声が聞き取れませんでした]"

        # 採点 (Groq Llama)
        prompt_llama = prompt_evaluation + f"\n【生徒の回答】: {student_speech}\n\n【出力フォーマット】\n必ず以下の形式で出力してください。\n\n判定: (A / B / C のいずれか)\nアドバイス: (生徒への優しい日本語のアドバイス)"
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_llama}],
            model="llama-3.1-8b-instant",
            temperature=0.3,
        )
        eval_result = chat_completion.choices[0].message.content.strip()
        
        return student_speech, eval_result, "🟢 Groq (正常)"

    except Exception as groq_error:
        # Groqが制限やエラーで落ちた場合、自動でGemini（ルート②）に突入
        pass

    # --- ルート②: バックアップの Gemini で実行 ---
    try:
        prompt_gemini = prompt_evaluation + """
        \n添付された生徒の録音音声（英語）を聴いて、以下の2つのタスクを行ってください。

        【タスク1: 文字起こし】
        生徒が何と言っているか、英語で正確に文字起こししてください。

        【タスク2: 採点・評価】
        判定（A/B/C）と優しい日本語アドバイスを作成してください。

        【出力フォーマット】
        必ず以下の形式で出力してください。
        ■文字起こし:
        (ここに文字起こしした英文)
        ■評価結果:
        判定: (A / B / C)
        アドバイス: (日本語アドバイス)
        """
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([
            {'mime_type': 'audio/wav', 'data': audio_bytes},
            prompt_gemini
        ])
        
        result_text = response.text
        student_speech_gemini = "[文字起こしの抽出に失敗しました]"
        eval_result_gemini = result_text
        
        if "■文字起こし:" in result_text and "■評価結果:" in result_text:
            parts = result_text.split("■評価結果:")
            eval_result_gemini = "■評価結果:" + parts[1]
            student_speech_gemini = parts[0].replace("■文字起こし:", "").strip()
            
        return student_speech_gemini, eval_result_gemini, "🟡 Gemini (バックアップ発動)"
        
    except Exception as gemini_error:
        # 両方全滅した場合
        return f"[エラー] 両方のAIが応答しませんでした。", f"Groqエラー: {groq_error}\nGeminiエラー: {gemini_error}", "🔴 全滅"

def upload_to_drive(audio_bytes, file_name) -> str:
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
        return f"Upload Failed: {e}"

# --- 4. スプレッドシート操作関数 ---
@st.cache_data(ttl=600)
def load_all_config():
    try:
        sh = get_spreadsheet()
        data = sh.worksheet("Config").get_all_records()
        return pd.DataFrame(data)
    except:
        return pd.DataFrame()

def save_results_to_sheet(student_info: dict, answers: dict, time_records: dict, num_questions: int):
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
    try:
        sh = get_spreadsheet()
        sh.worksheet("Results").append_row(row_data)
        st.success("結果がスプレッドシートに保存されました。")
    except Exception as e:
        st.error(f"保存エラー: {e}")

# --- 5. メイン処理 ---
st.title("🇬🇧 AI English QA Test (Hybrid)")

df_config_all = load_all_config()

if not st.session_state.test_started:
    st.subheader("受験者情報を入力してください")
    if not df_config_all.empty and 'School' in df_config_all.columns:
        df_config_all = df_config_all.astype(str)
        available_schools = sorted(list(df_config_all['School'].dropna().unique()))
        available_grades = sorted(list(df_config_all['Grade'].dropna().unique()))
        available_classes = sorted(list(df_config_all['Class'].dropna().unique()))
    else:
        available_schools, available_grades, available_classes = ["〇〇中"], ["1年"], ["1組"]
    
    school = st.selectbox("学校名", available_schools)
    grade = st.selectbox("学年", available_grades)
    class_num = st.selectbox("クラス", available_classes)
    attend_num = st.selectbox("出席番号", [i for i in range(1, 51)], index=0)
    name = st.text_input("氏名（例：タロウ / ニックネーム）")
    
    if st.button("テストを始める", type="primary"):
        if name.strip() == "" or df_config_all.empty:
            st.warning("入力確認またはConfigシートを確認してください。")
        else:
            df_config_all = df_config_all.astype(str)
            student_config = df_config_all[(df_config_all['School'] == str(school)) & (df_config_all['Grade'] == str(grade)) & (df_config_all['Class'] == str(class_num))]
            if student_config.empty:
                st.error("クラス設定が見つかりません。")
            else:
                st.session_state.student_info = {"school": school, "grade": grade, "class_num": class_num, "attend_num": attend_num, "name": name.strip(), "config": student_config.iloc[0].to_dict()}
                st.session_state.test_started = True
                st.session_state.current_q_idx = 1
                st.session_state.answers_cache = {}
                st.session_state.time_records = {1:0, 2:0, 3:0, 4:0, 5:0}
                st.session_state.current_feedback = None
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
        st.markdown("#### 🗣️ 2. あなたの回答を録音してください")
        
        if st.session_state.current_feedback is None:
            audio_file = st.audio_input("ここを押して発話・録音", key=f"audio_{idx}")
            
            if st.button("回答を送信する", type="primary", key=f"submit_{idx}"):
                if audio_file is None:
                    st.warning("音声が録音されていません。")
                else:
                    st.session_state.time_records[idx] = int(time.time() - st.session_state.start_time)
                    with st.spinner("AIがあなたの英語を分析中... 📝"):
                        audio_bytes = audio_file.read()
                        info = st.session_state.student_info
                        file_name = f"{info['grade']}{info['class_num']}_{info['attend_num']}番_{info['name']}_Q{idx}.wav"
                        audio_url = upload_to_drive(audio_bytes, file_name)
                        
                        # ハイブリッド評価関数を実行
                        student_speech, eval_result, system_status = analyze_and_evaluate_hybrid(audio_bytes, q_text, q_criteria)
                        
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
                st.caption(f"🔧 稼働システム情報: {st.session_state.current_feedback['status']}") # 動作確認用の隠し文字
            
            if st.button("次の質問へ進む ➡️", type="primary", key=f"next_btn_{idx}"):
                st.session_state.current_feedback = None
                st.session_state.start_time = None 
                st.session_state.current_q_idx += 1
                st.rerun()
    else:
        st.balloons()
        st.success("🎉 すべての質問が終了しました！")
        if "data_saved" not in st.session_state:
            with st.spinner("保存中..."):
                save_results_to_sheet(st.session_state.student_info, st.session_state.answers_cache, st.session_state.time_records, num_questions)
            st.session_state.data_saved = True
        if st.button("最初の画面に戻る（次の生徒用）"):
            for key in list(st.session_state.keys()): del st.session_state[key]
            st.rerun()

st.markdown("---")
st.markdown("<div style='text-align: center; color: #888888; font-size: 0.8em;'>© 2026 Shogo Takeuchi. All Rights Reserved.</div>", unsafe_allow_html=True)
