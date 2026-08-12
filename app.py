import os
import time
import asyncio
import threading
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS

import edge_tts
from groq import Groq

app = Flask(__name__)
CORS(app)

# ---------- Configurações (vêm das variáveis de ambiente do Render) ----------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE_NAME = os.environ.get("VOICE_NAME", "pt-BR-AntonioNeural")  # troque para pt-BR-FranciscaNeural se quiser voz feminina
MIN_SECONDS_BETWEEN_ANSWERS = float(os.environ.get("MIN_SECONDS_BETWEEN_ANSWERS", "8"))
MIN_MESSAGE_LENGTH = int(os.environ.get("MIN_MESSAGE_LENGTH", "4"))

# Expressões que costumam indicar um pedido de explicação/dúvida, mesmo sem "?"
# (ex: "me ensina o que é PNL", "explica como funciona isso", "não entendi essa parte")
PALAVRAS_DE_PERGUNTA = [
    "o que é", "o que e", "o que seria", "o que significa",
    "como funciona", "como faz", "como fazer", "como se faz",
    "por que", "porque", "pra que serve", "para que serve",
    "qual é", "qual e", "quais são", "quais sao", "quem foi", "quem é", "quem e",
    "me explica", "me explique", "explica pra mim", "explique pra mim",
    "me ensina", "me ensine", "ensina pra mim",
    "me diz", "me diga", "me conta", "conta pra mim", "fala sobre", "fale sobre",
    "não entendi", "nao entendi", "não entendo", "nao entendo",
    "me fala", "me fale",
]

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

# Classificação de presentes por nível (baseado nos presentes mais comuns do TikTok Live).
# Presentes não listados aqui caem em "barato" por padrão (comportamento seguro/rápido).
GIFT_TIERS = {
    # baratos (até ~10 moedas)
    "rosa": "barato", "rose": "barato",
    "gg": "barato",
    "coração": "barato", "coracao": "barato", "heart": "barato",
    "dedo em riste": "barato", "finger heart": "barato",
    "pulseira de amizade": "barato",
    "confete": "barato",
    # médios (~50 a 500 moedas)
    "panda": "medio", "boneco de neve": "medio", "cachorro fofo": "medio",
    "sorvete": "medio", "ice cream": "medio",
    "capacete de festa": "medio", "microfone dourado": "medio",
    "câmera": "medio",
    "coelhinho": "medio",
    # caros (1000+ moedas)
    "leão": "caro", "leao": "caro", "lion": "caro",
    "universo": "caro", "universe": "caro",
    "foguete": "caro", "rocket": "caro",
    "carro esportivo": "caro", "sports car": "caro",
    "castelo": "caro",
    "galáxia": "caro", "galaxia": "caro", "galaxy": "caro",
}

# NOVO: mapeia cada tier de presente pro nome do clipe de vídeo do avatar
# que deve ser exibido no player.html enquanto a fala toca.
# Os nomes aqui devem bater exatamente com os arquivos de vídeo em static/video/
TIER_TO_AVATAR_CLIP = {
    "barato": "avatar_presente_barato",
    "medio": "avatar_presente_medio",
    "caro": "avatar_presente_caro",
}

# Clipe usado quando o avatar está respondendo uma pergunta do chat
AVATAR_CLIP_PERGUNTA = "avatar_pergunta"

# Clipe padrão de descanso (looping) — o player volta pra ele sozinho
# assim que o áudio da fala termina de tocar
AVATAR_CLIP_IDLE = "avatar_idle"


def classificar_presente(gift_name: str) -> str:
    chave = gift_name.strip().lower()
    return GIFT_TIERS.get(chave, "barato")


AUDIO_DIR = os.path.join(os.path.dirname(__file__), "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# NOVO: pasta onde ficam os vídeos do avatar (idle, pergunta, presente_barato,
# presente_medio, presente_caro, transicao). Coloque os arquivos .mp4 aqui,
# com esses nomes exatos (ex: avatar_idle.mp4, avatar_pergunta.mp4...).
VIDEO_DIR = os.path.join(os.path.dirname(__file__), "static", "video")
os.makedirs(VIDEO_DIR, exist_ok=True)

_last_answer_time = 0.0
_lock = threading.Lock()
_queue = []  # fila de áudios esperando pra tocar, na ordem em que chegaram
_queue_lock = threading.Lock()
_next_id = 0
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "20"))


def enfileirar(texto: str, nome_arquivo: str, avatar_clip: str = AVATAR_CLIP_IDLE):
    global _next_id
    with _queue_lock:
        if len(_queue) >= MAX_QUEUE_SIZE:
            return False  # fila cheia, ignora pra não deixar a live num monólogo infinito
        _next_id += 1
        _queue.append({
            "id": _next_id,
            "text": texto,
            "filename": nome_arquivo,
            "avatar_clip": avatar_clip,  # NOVO: diz ao player.html qual vídeo mostrar
        })
        return True


def gerar_audio(texto: str, caminho_saida: str, voice: str = None, pitch: str = "+0Hz", rate: str = "+0%"):
    voice = voice or VOICE_NAME

    async def _run():
        communicate = edge_tts.Communicate(texto, voice, pitch=pitch, rate=rate)
        await communicate.save(caminho_saida)
    asyncio.run(_run())


def gerar_resposta_e_falar(system_prompt: str, prompt_usuario: str, prefixo_arquivo: str = "resposta",
                            voice: str = None, pitch: str = "+0Hz", rate: str = "+0%",
                            avatar_clip: str = AVATAR_CLIP_IDLE):
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

    enfileirar(resposta_texto, nome_arquivo, avatar_clip=avatar_clip)

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
    parece_pedido_explicacao = any(expressao in mensagem_lower for expressao in PALAVRAS_DE_PERGUNTA)

    if not (parece_pergunta or usou_comando or parece_pedido_explicacao):
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
        resposta_texto, nome_arquivo = gerar_resposta_e_falar(
            SYSTEM_PROMPT, prompt_usuario, prefixo_arquivo="resposta",
            avatar_clip=AVATAR_CLIP_PERGUNTA,  # NOVO
        )

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        return jsonify({
            "skipped": False,
            "text": resposta_texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
            "avatar_clip": AVATAR_CLIP_PERGUNTA,  # NOVO
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

    tier = classificar_presente(gift_name)
    avatar_clip = TIER_TO_AVATAR_CLIP.get(tier, TIER_TO_AVATAR_CLIP["barato"])  # NOVO
    vai_usar_ia = (GIFT_MODE != "npc") or (tier == "caro")

    # Presentes caros (que usam IA) respeitam o cooldown mais longo, mesmo em modo NPC.
    # Presentes baratos/médios em modo NPC usam um cooldown bem curto, já que a fila cuida do resto.
    cooldown = MIN_SECONDS_BETWEEN_GIFTS if vai_usar_ia else MIN_SECONDS_BETWEEN_GIFTS_NPC

    if vai_usar_ia:
        with _lock:
            now = time.time()
            if now - _last_gift_time < cooldown:
                return jsonify({"skipped": True, "reason": "rate_limited"}), 200
            _last_gift_time = now

    try:
        if not vai_usar_ia:
            # Modo NPC clássico: repete o nome (mais forte quanto maior o nível), sem passar pela IA
            if tier == "medio":
                resposta_texto = f"{gift_name}! {gift_name}!"
            else:
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
            colocou_na_fila = enfileirar(resposta_texto, nome_arquivo, avatar_clip=avatar_clip)  # NOVO
            if not colocou_na_fila:
                return jsonify({"skipped": True, "reason": "queue_full"}), 200

        else:
            # Modo IA, ou presente CARO mesmo estando em modo NPC: reação especial e elaborada
            if tier == "caro":
                prompt_usuario = (
                    f"{username} acabou de mandar o presente '{gift_name}', que é um dos presentes "
                    f"mais caros e especiais da live! Reaja com bastante empolgação e gratidão, "
                    f"como se fosse algo realmente incrível e raro."
                )
            else:
                prompt_usuario = f"{username} acabou de mandar o presente '{gift_name}'."

            resposta_texto, nome_arquivo = gerar_resposta_e_falar(
                GIFT_SYSTEM_PROMPT,
                prompt_usuario,
                prefixo_arquivo="presente",
                voice="pt-BR-FranciscaNeural",
                pitch="+55Hz",
                rate="+10%",
                avatar_clip=avatar_clip,  # NOVO
            )

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        return jsonify({
            "skipped": False,
            "mode": GIFT_MODE,
            "text": resposta_texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
            "tier": tier,  # NOVO — útil pra debug/teste
            "avatar_clip": avatar_clip,  # NOVO
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


@app.route("/next")
def next_in_queue():
    with _queue_lock:
        if not _queue:
            return jsonify({"empty": True})
        item = _queue.pop(0)
        return jsonify({"empty": False, **item})


@app.route("/static/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


# NOVO: rota pra servir os vídeos do avatar (idle, pergunta, presentes, transição)
@app.route("/static/video/<path:filename>")
def serve_video(filename):
    return send_from_directory(VIDEO_DIR, filename)


@app.route("/teste.html")
def pagina_teste():
    return send_from_directory(os.path.dirname(__file__), "teste.html")


@app.route("/player")
def player():
    return render_template("player.html")


@app.route("/")
def home():
    return "Servidor da IA da live está no ar. Use /player como Browser Source no OBS."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
