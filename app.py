import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
import datetime
import io
import requests
from openai import AzureOpenAI
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account

# --- 1. ページ基本設定 & セッション状態の初期化 ---
st.set_page_config(page_title="AI英語QAテスト (Azure)", page_icon="🇬🇧", layout="centered")

if "test_started" not in st.session_state:
    st.session_state.test_started = False
if "current_q_idx" not in st.session_state:
    st.session_state.current_q_idx = 0
if "student_info" not in st.session_state:
    st.session_state.student_info = {}
if "answers_cache" not in st.session_state:
    st.session_state.answers_cache = {}

# エンドポイントとキーの基本情報
base_endpoint = st.secrets["AZURE_OPENAI_ENDPOINT"].strip().rstrip("/")
api_key = st.secrets["AZURE_OPENAI_API_KEY"]

# チャット（gpt-4o-mini）用クライアント
client_chat = AzureOpenAI(
    api_key=api_key,
    api_version=st.secrets.get("AZURE_OPENAI_API_VERSION_CHAT", "2024-08-01-preview"),
    azure_endpoint=base_endpoint
)

# Googleドライブの保存先フォルダID
GOOGLE_DRIVE_FOLDER_ID = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]

# スプレッドシート接続
conn = st.connection("gsheets", type=GSheetsConnection)

# --- 2. Azure OpenAI 連携関数 ---

def generate_ai_voice(text: str):
    """【Azure OpenAI】HTTPリクエストで確実にTTS音声を生成"""
    try:
        deployment_tts = st.secrets["AZURE_DEPLOYMENT_TTS"]
        version_audio = st.secrets.get("AZURE_OPENAI_API_VERSION_AUDIO", "2024-02-15-preview")
        
        # Azure専用のTTSエンドポイントURLを手動構築
        url = f"{base_endpoint}/openai/deployments/{deployment_tts}/audio/speech?api-version={version_audio}"
        
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "model": deployment_tts,
            "input": text,
            "voice": "alloy"
        }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            return response.content
        else:
            st.error(f"TTS通信エラー ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        st.error(f"AI音声の生成に失敗しました: {e}")
        return None

def transcribe_audio(audio_bytes) -> str:
    """【Azure OpenAI】HTTPリクエストで確実にWhisper文字起こし"""
    try:
        deployment_whisper = st.secrets["AZURE_DEPLOYMENT_WHISPER"]
        version_audio = st.secrets.get("AZURE_OPENAI_API_VERSION_AUDIO", "2024-02-15-preview")
        
        # Azure専用のWhisperエンドポイントURLを手動構築
        url = f"{base_endpoint}/openai/deployments/{deployment_whisper}/audio/transcriptions?api-version={version_audio}"
        
        headers = {
            "api-key": api_key
        }
        files = {
            "file": ("speech.wav", io.BytesIO(audio_bytes), "audio/wav")
        }
        
        response = requests.post(url, headers=headers, files=files)
        if response.status_code == 200:
            return response.json().get("text", "")
        else:
            return f"[文字起こしエラー ({response.status_code}): {response.text}]"
    except Exception as e:
        return f"[文字起こし失敗: {e}]"

def evaluate_speech(student_text: str, question_text: str, criteria: str) -> str:
    """【Azure OpenAI】ChatGPTによる文法チェック・採点"""
    try:
        prompt = f"""
        あなたは中学校の英語教師です。生徒の回答を採点・評価してください。
        質問: {question_text}
        生徒の回答: {student_text}
        
        【採点・評価基準】
        {criteria}
        
        上記基準に基づき、判定（A/B/C）と、生徒への優しいアドバイス（日本語）を出力してください。
        """
        response = client_chat.chat.completions.create(
            model=st.secrets["AZURE_DEPLOYMENT_CHAT"],
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[AI採点失敗: {e}]"

def upload_to_drive(audio_bytes, file_name) -> str:
    """Googleドライブの指定フォルダへ音声をアップロードし、URLを返す"""
    try:
        creds_info = st.secrets["connections"]["gsheets"]
        scopes = ['https://www.googleapis.com/auth/drive.file']
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
        drive_service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {'name': file_name, 'parents': [GOOGLE_DRIVE_FOLDER_ID]}
        media = MediaIoBaseUpload(io.BytesIO(audio_bytes), mimetype='audio/wav', resumable=True)
        
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        return file.get('webViewLink', '')
    except Exception as e:
        st.error(f"Googleドライブへのアップロードに失敗しました: {e}")
        return "Upload Failed"

# --- 3. スプレッドシート操作関数 ---

def load_all_config():
    """Configシートを全件取得"""
    return conn.read(worksheet="Config", ttl=0)

def save_all_config(df_config):
    """Configシート全体を上書き保存"""
    conn.update(worksheet="Config", data=df_config)
    st.cache_data.clear()

def save_results_to_sheet(student_info: dict, answers: dict, num_questions: int):
    """Resultsシートへ1人1行（横並び）でデータを追加保存"""
    row_data = {
        "Timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "School": student_info.get("school"),
        "Grade": student_info.get("grade"),
        "Class": student_info.get("class_num"),
        "Number": student_info.get("attend_num"),
        "Name": student_info.get("name"),
    }
    
    for i in range(1, 6):
        if i <= num_questions:
            row_data[f"Q{i}_Speech"] = answers.get(f"q{i}_speech", "")
            row_data[f"Q{i}_Eval"] = answers.get(f"q{i}_eval", "")
            row_data[f"Q{i}_Audio_Link"] = answers.get(f"q{i}_audio_url", "")
        else:
            row_data[f"Q{i}_Speech"] = ""
            row_data[f"Q{i}_Eval"] = ""
            row_data[f"Q{i}_Audio_Link"] = ""
            
    try:
        existing_df = conn.read(worksheet="Results", ttl=0)
        new_df = pd.DataFrame([row_data])
        updated_df = pd.concat([existing_df, new_df], ignore_index=True)
        conn.update(worksheet="Results", data=updated_df)
        st.success("テスト結果がスプレッドシートに正常に保存されました。")
    except Exception as e:
        st.error(f"結果の保存中にエラーが発生しました: {e}")

# --- 4. メインルーティング ---
st.sidebar.title("メニュー")
mode = st.sidebar.radio("画面を選択してください", ["生徒用テスト画面", "先生用管理画面"])

# 全体の設定データを事前に読み込み
df_config_all = load_all_config()

# --- 5. 先生用管理画面 ---
if mode == "先生用管理画面":
    st.title("🛠️ 先生用管理画面 (Azure)")
    
    st.subheader("設定対象のクラスを選択してください")
    tgt_school = st.text_input("学校名", value="〇〇中学校")
    tgt_grade = st.selectbox("学年", ["1年", "2年", "3年"], key="m_grade")
    tgt_class = st.selectbox("クラス", [f"{i}組" for i in range(1, 6)], key="m_class")
    
    # 選択されたクラスの行を抽出
    match_row = df_config_all[
        (df_config_all['School'] == tgt_school) & 
        (df_config_all['Grade'] == tgt_grade) & 
        (df_config_all['Class'] == tgt_class)
    ]
    
    if not match_row.empty:
        correct_password = str(match_row.iloc[0]['Admin_Password'])
        current_config = match_row.iloc[0].to_dict()
    else:
        correct_password = "password123"
        current_config = {"num_questions": 3}
        
    input_password = st.text_input("このクラスの設定用パスワードを入力してください", type="password")
    
    if input_password == correct_password:
        st.success(f"🔓 認証成功: {tgt_school} {tgt_grade}{tgt_class} 設定画面")
        st.markdown("---")
        
        new_password = st.text_input("管理用パスワード（スプレッドシート内を上書き変更）", value=correct_password)
        
        try:
            init_num = int(current_config.get("num_questions", 3))
        except:
            init_num = 3
        new_num = st.selectbox("質問数", options=[1, 2, 3, 4, 5], index=init_num-1)
        
        # 保存データの土台作成
        updated_row_dict = {
            "School": tgt_school, "Grade": tgt_grade, "Class": tgt_class,
            "Admin_Password": new_password, "num_questions": new_num
        }
        
        for i in range(1, 6):
            if i <= new_num:
                st.markdown(f"##### 📋 質問 {i}")
                def_text = current_config.get(f"q{i}_text", f"Question {i}?") if not match_row.empty else f"Question {i}?"
                def_crit = current_config.get(f"q{i}_criteria", "正しく答えられているか。") if not match_row.empty else "判定基準を入力"
                
                updated_row_dict[f"q{i}_text"] = st.text_input(f"Q{i} 英語テキスト", value=def_text, key=f"t_{i}")
                updated_row_dict[f"q{i}_criteria"] = st.text_area(f"Q{i} 評価基準", value=def_crit, key=f"c_{i}")
            else:
                updated_row_dict[f"q{i}_text"] = ""
                updated_row_dict[f"q{i}_criteria"] = ""
                
        if st.button("このクラスの設定を保存・更新する", type="primary"):
            with st.spinner("スプレッドシートを更新中..."):
                if not match_row.empty:
                    df_config_all = df_config_all.drop(match_row.index)
                new_row_df = pd.DataFrame([updated_row_dict])
                df_config_all = pd.concat([df_config_all, new_row_df], ignore_index=True)
                
                save_all_config(df_config_all)
            st.success("設定を更新しました！")
            st.rerun()
            
    elif input_password != "":
        st.error("パスワードが正しくありません。")

# --- 6. 生徒用テスト画面 ---
else:
    st.title("🇬🇧 AI English QA Test")
    
    if not st.session_state.test_started:
        st.subheader("受験者情報を入力してください")
        
        available_schools = sorted(list(df_config_all['School'].dropna().unique())) if not df_config_all.empty else ["〇〇中学校"]
        available_grades = sorted(list(df_config_all['Grade'].dropna().unique())) if not df_config_all.empty else ["1年", "2年", "3年"]
        available_classes = sorted(list(df_config_all['Class'].dropna().unique())) if not df_config_all.empty else ["1組", "2組", "3組"]
        
        school = st.selectbox("学校名", available_schools)
        grade = st.selectbox("学年", available_grades)
        class_num = st.selectbox("クラス", available_classes)
        attend_num = st.selectbox("出席番号", [i for i in range(1, 51)], index=0)
        name = st.text_input("氏名")
        
        if st.button("テストを始める", type="primary"):
            if name.strip() == "":
                st.warning("氏名を入力してください。")
            else:
                student_config = df_config_all[
                    (df_config_all['School'] == school) & 
                    (df_config_all['Grade'] == grade) & 
                    (df_config_all['Class'] == class_num)
                ]
                
                if student_config.empty:
                    st.error("選択したクラスの設定がありません。先生画面で先に問題を作成してください。")
                else:
                    st.session_state.student_info = {
                        "school": school, "grade": grade, "class_num": class_num,
                        "attend_num": attend_num, "name": name.strip(),
                        "config": student_config.iloc[0].to_dict()
                    }
                    st.session_state.test_started = True
                    st.session_state.current_q_idx = 1
                    st.session_state.answers_cache = {}
                    st.rerun()

    else:
        student_config = st.session_state.student_info["config"]
        num_questions = int(student_config.get("num_questions", 3))
        idx = st.session_state.current_q_idx
        
        if idx <= num_questions:
            st.markdown(f"### 🚀 Question {idx} / {num_questions}")
            q_text = student_config.get(f"q{idx}_text", "")
            q_criteria = student_config.get(f"q{idx}_criteria", "")
            
            voice_key = f"ai_voice_{idx}"
            if voice_key not in st.session_state:
                with st.spinner("AIが質問を準備しています... 🎧"):
                    st.session_state[voice_key] = generate_ai_voice(q_text)
            
            st.markdown("#### 🎧 1. AIの質問を聴いてください")
            if st.session_state[voice_key]:
                st.audio(st.session_state[voice_key], format="audio/mp3")
            
            st.info(f"👉 (画面補助テキスト): {q_text}")
            
            st.markdown("---")
            st.markdown("#### 🗣️ 2. あなたの回答を録音してください")
            audio_file = st.audio_input("ここを押して発話・録音", key=f"audio_{idx}")
            
            if st.button("回答を送信して次へ進む", type="primary", key=f"submit_{idx}"):
                if audio_file is None:
                    st.warning("音声が録音されていません。")
                else:
                    with st.spinner("音声を分析して、次の問題へ移動しています..."):
                        audio_bytes = audio_file.read()
                        info = st.session_state.student_info
                        
                        file_name = f"{info['grade']}{info['class_num']}_{info['attend_num']}番_{info['name']}_Q{idx}.wav"
                        audio_url = upload_to_drive(audio_bytes, file_name)
                        
                        student_speech = transcribe_audio(audio_bytes)
                        eval_result = evaluate_speech(student_speech, q_text, q_criteria)
                        
                        st.session_state.answers_cache[f"q{idx}_speech"] = student_speech
                        st.session_state.answers_cache[f"q{idx}_eval"] = eval_result
                        st.session_state.answers_cache[f"q{idx}_audio_url"] = audio_url
                        
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
                        num_questions
                    )
                st.session_state.data_saved = True
                
            st.markdown("---")
            if st.button("最初の画面に戻る（次の生徒の受験用）"):
                for key in list(st.session_state.keys()):
                    del st.session_state[key]
                st.rerun()
