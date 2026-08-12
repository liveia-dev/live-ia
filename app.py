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

GIFT_SYSTEM_PROMPT = (
    "Você é o assistente de voz de uma live de bate-papo descontraída no TikTok. "
    "Alguém acabou de te mandar um presente virtual durante a live. "
    "Agradeça de forma curta (1 frase, no máximo 2), animada e criativa, mencionando o nome da "
    "pessoa e, se fizer sentido, o nome do presente. Varie as frases, não repita sempre o mesmo agradecimento. "
    "Nunca use palavrão, conteúdo sexual, ofensivo ou pesado. Fale em português do Brasil."
)

MIN_SECONDS_BETWEEN_GIFTS = float(os.environ.get("MIN_SECONDS_BETWEEN_GIFTS", "5"))
_last_gift_time = 0.0

GIFT_MODE = os.environ.get("GIFT_MODE", "ai").strip().lower()  # "ai" ou "npc"
MIN_SECONDS_BETWEEN_GIFTS_NPC = float(os.environ.get("MIN_SECONDS_BETWEEN_GIFTS_NPC", "1.5"))

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

_last_answer_time = 0.0
_lock = threading.Lock()
_latest = {"filename": None, "text": None, "id": 0}


def gerar_audio(texto: str, caminho_saida: str, voice: str = None, pitch: str = "+0Hz", rate: str = "+0%"):
    voice = voice or VOICE_NAME

    async def _run():
        communicate = edge_tts.Communicate(texto, voice, pitch=pitch, rate=rate)
        await communicate.save(caminho_saida)
    asyncio.run(_run())


def gerar_resposta_e_falar(system_prompt: str, prompt_usuario: str, prefixo_arquivo: str = "resposta",
                            voice: str = None, pitch: str = "+0Hz", rate: str = "+0%"):
    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_usuario},
        ],
        max_tokens=150,
        temperature=0.8,
    )
    resposta_texto = completion.choices[0].message.content.strip()

    nome_arquivo = f"{prefixo_arquivo}_{int(time.time() * 1000)}.mp3"
    caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
    gerar_audio(resposta_texto, caminho_completo, voice=voice, pitch=pitch, rate=rate)

    _latest["filename"] = nome_arquivo
    _latest["text"] = resposta_texto
    _latest["id"] += 1

    return resposta_texto, nome_arquivo


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
        resposta_texto, nome_arquivo = gerar_resposta_e_falar(SYSTEM_PROMPT, prompt_usuario, prefixo_arquivo="resposta")

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        return jsonify({
            "skipped": False,
            "text": resposta_texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
        })

    except Exception as e:
        return jsonify({"skipped": True, "reason": "error", "detail": str(e)}), 500


@app.route("/gift", methods=["POST"])
def gift():
    global _last_gift_time

    data = request.get_json(force=True, silent=True) or {}

    # value1 = username, value3 = nome do presente (SKU), conforme o webhook do TikFinity
    username = str(data.get("value1", data.get("username", "alguém"))).strip() or "alguém"
    gift_name = str(data.get("value3", data.get("gift", ""))).strip() or "um presente"

    cooldown = MIN_SECONDS_BETWEEN_GIFTS_NPC if GIFT_MODE == "npc" else MIN_SECONDS_BETWEEN_GIFTS

    with _lock:
        now = time.time()
        if now - _last_gift_time < cooldown:
            return jsonify({"skipped": True, "reason": "rate_limited"}), 200
        _last_gift_time = now

    try:
        if GIFT_MODE == "npc":
            # Modo NPC clássico: só repete o nome do presente, sem passar pela IA (mais rápido e mais barato)
            resposta_texto = gift_name

            nome_arquivo = f"presente_{int(time.time() * 1000)}.mp3"
            caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
            gerar_audio(
                resposta_texto,
                caminho_completo,
                voice="pt-BR-FranciscaNeural",
                pitch="+55Hz",
                rate="+10%",
            )
            _latest["filename"] = nome_arquivo
            _latest["text"] = resposta_texto
            _latest["id"] += 1

        else:
            # Modo IA: agradecimento criativo gerado pela Groq
            prompt_usuario = f"{username} acabou de mandar o presente '{gift_name}'."
            resposta_texto, nome_arquivo = gerar_resposta_e_falar(
                GIFT_SYSTEM_PROMPT,
                prompt_usuario,
                prefixo_arquivo="presente",
                voice="pt-BR-FranciscaNeural",
                pitch="+55Hz",
                rate="+10%",
            )

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        return jsonify({
            "skipped": False,
            "mode": GIFT_MODE,
            "text": resposta_texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
        })

    except Exception as e:
        return jsonify({"skipped": True, "reason": "error", "detail": str(e)}), 500


@app.route("/sample-gift-voice")
def sample_gift_voice():
    style = request.args.get("style", "esquilo")

    frase = "Aiii, muito obrigada pelo presente, você é incrível!"

    estilos = {
        "esquilo": {"voice": "pt-BR-AntonioNeural", "pitch": "+35Hz", "rate": "+25%"},
        "dramatico": {"voice": "pt-BR-AntonioNeural", "pitch": "-15Hz", "rate": "-15%"},
        "normal": {"voice": "pt-BR-AntonioNeural", "pitch": "+0Hz", "rate": "+0%"},
        "robo": {"voice": "pt-BR-AntonioNeural", "pitch": "-40Hz", "rate": "-30%"},
        "gringo": {"voice": "en-US-GuyNeural", "pitch": "+0Hz", "rate": "+0%"},
        "fininha": {"voice": "pt-BR-FranciscaNeural", "pitch": "+55Hz", "rate": "+10%"},
    }

    config = estilos.get(style, estilos["esquilo"])

    nome_arquivo = f"amostra_{style}_{int(time.time() * 1000)}.mp3"
    caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
    gerar_audio(frase, caminho_completo, voice=config["voice"], pitch=config["pitch"], rate=config["rate"])

    base_url = request.host_url.rstrip("/")
    audio_url = f"{base_url}/static/audio/{nome_arquivo}"

    return f"""
    <html><body style="font-family:sans-serif; text-align:center; margin-top:50px;">
        <h2>Amostra: {style}</h2>
        <audio controls autoplay src="{audio_url}"></audio>
        <p><a href="/sample-gift-voice?style=esquilo">Esquilo</a> |
           <a href="/sample-gift-voice?style=dramatico">Dramático</a> |
           <a href="/sample-gift-voice?style=normal">Normal</a> |
           <a href="/sample-gift-voice?style=robo">Robô</a> |
           <a href="/sample-gift-voice?style=gringo">Gringo</a> |
           <a href="/sample-gift-voice?style=fininha">Fininha</a></p>
    </body></html>
    """


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
