import os
import time
import asyncio
import threading
from flask import Flask, request, jsonify, send_from_directory, render_template

import edge_tts
from groq import Groq

app = Flask(__name__)

# ---------- Configurações (vêm das variáveis de ambiente do Render) ----------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE_NAME = os.environ.get("VOICE_NAME", "pt-BR-AntonioNeural")  # troque para pt-BR-FranciscaNeural se quiser voz feminina
MIN_SECONDS_BETWEEN_ANSWERS = float(os.environ.get("MIN_SECONDS_BETWEEN_ANSWERS", "8"))
MIN_MESSAGE_LENGTH = int(os.environ.get("MIN_MESSAGE_LENGTH", "4"))

client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = (
    "Você é o assistente de voz de uma live de bate-papo descontraída no TikTok. "
    "Responda às perguntas do chat de forma curta (no máximo 2 a 3 frases), "
    "leve, simpática e com bom humor, em português do Brasil. "
    "Nunca use palavrão, conteúdo sexual, ofensivo ou pesado. "
    "Se a pergunta for confusa ou incompleta, responda de forma engraçada e gentil pedindo pra repetir. "
    "Fale como se estivesse conversando de verdade com a pessoa, sem enrolação."
)

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

_last_answer_time = 0.0
_lock = threading.Lock()
_latest = {"filename": None, "text": None, "id": 0}


def gerar_audio(texto: str, caminho_saida: str):
    async def _run():
        communicate = edge_tts.Communicate(texto, VOICE_NAME)
        await communicate.save(caminho_saida)
    asyncio.run(_run())


@app.route("/ask", methods=["POST"])
def ask():
    global _last_answer_time

    data = request.get_json(force=True, silent=True) or {}

    # Aceita tanto o formato do TikFinity (value1/value2) quanto o nosso formato de teste (username/message)
    username = str(data.get("value1", data.get("username", ""))).strip()
    message = str(data.get("value2", data.get("message", ""))).strip()

    if len(message) < MIN_MESSAGE_LENGTH:
        return jsonify({"skipped": True, "reason": "message_too_short"}), 200

    mensagem_lower = message.lower().strip()
    parece_pergunta = "?" in message
    usou_comando = mensagem_lower.startswith("!pergunta")

    if not (parece_pergunta or usou_comando):
        return jsonify({"skipped": True, "reason": "not_a_question"}), 200

    if usou_comando:
        # remove o "!pergunta" do começo, deixando só o texto da dúvida
        message = message[len("!pergunta"):].strip(" :,-")
        if len(message) < MIN_MESSAGE_LENGTH:
            return jsonify({"skipped": True, "reason": "message_too_short"}), 200

    with _lock:
        now = time.time()
        if now - _last_answer_time < MIN_SECONDS_BETWEEN_ANSWERS:
            return jsonify({"skipped": True, "reason": "rate_limited"}), 200
        _last_answer_time = now

    try:
        prompt_usuario = message if not username else f"{username} perguntou: {message}"

        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_usuario},
            ],
            max_tokens=150,
            temperature=0.8,
        )
        resposta_texto = completion.choices[0].message.content.strip()

        nome_arquivo = f"resposta_{int(time.time() * 1000)}.mp3"
        caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
        gerar_audio(resposta_texto, caminho_completo)

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        _latest["filename"] = nome_arquivo
        _latest["text"] = resposta_texto
        _latest["id"] += 1

        return jsonify({
            "skipped": False,
            "text": resposta_texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
        })

    except Exception as e:
        return jsonify({"skipped": True, "reason": "error", "detail": str(e)}), 500


@app.route("/latest")
def latest():
    return jsonify(_latest)


@app.route("/static/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


@app.route("/player")
def player():
    return render_template("player.html")


@app.route("/")
def home():
    return "Servidor da IA da live está no ar. Use /player como Browser Source no OBS."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
