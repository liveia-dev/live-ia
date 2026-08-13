import os
import re
import time
import asyncio
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
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

# NOVO: conexão direta com a live da TikTok, rodando no próprio Render — dispensa
# TikFinity/Streamer.bot rodando no seu PC. Ver bloco "listener da TikTok" no fim do arquivo.
TIKTOK_USERNAME = os.environ.get("TIKTOK_USERNAME", "").strip().lstrip("@")
TIKTOK_SIGN_API_KEY = os.environ.get("TIKTOK_SIGN_API_KEY", "").strip()  # opcional, chave da Euler Stream
INICIAR_LISTENER_TIKTOK = os.environ.get("INICIAR_LISTENER_TIKTOK", "true").strip().lower() == "true"

# Ajuste fino de pitch/rate da voz PRINCIPAL (a que responde perguntas no /ask),
# pra soar mais pausada e envolvente em vez do padrão "neutro" da Thalita.
# Dá pra testar outros valores direto no Render (env vars), sem mexer no código.
PITCH_PRINCIPAL = os.environ.get("PITCH_PRINCIPAL", "-10Hz")
RATE_PRINCIPAL = os.environ.get("RATE_PRINCIPAL", "-8%")

# Vozes de presente: fininha/acelerada pros baratos e médios (como já era),
# e GRAVE só pro presente CARO — a virada de chave que cria o efeito cômico
# de contraste (avatar fofa soltando timbre grosso do nada).
VOICE_PRESENTE_FININHA = os.environ.get("VOICE_PRESENTE_FININHA", "pt-BR-FranciscaNeural")
PITCH_PRESENTE_FININHA = os.environ.get("PITCH_PRESENTE_FININHA", "+55Hz")
RATE_PRESENTE_FININHA = os.environ.get("RATE_PRESENTE_FININHA", "+10%")

VOICE_PRESENTE_GRAVE = os.environ.get("VOICE_PRESENTE_GRAVE", "pt-BR-AntonioNeural")
PITCH_PRESENTE_GRAVE = os.environ.get("PITCH_PRESENTE_GRAVE", "-35Hz")
RATE_PRESENTE_GRAVE = os.environ.get("RATE_PRESENTE_GRAVE", "-15%")


def voz_para_presente(tier: str):
    """Escalada: normal (pergunta) -> fininha (barato/médio) -> GRAVE (caro)."""
    if tier == "caro":
        return VOICE_PRESENTE_GRAVE, PITCH_PRESENTE_GRAVE, RATE_PRESENTE_GRAVE
    return VOICE_PRESENTE_FININHA, PITCH_PRESENTE_FININHA, RATE_PRESENTE_FININHA

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

# Regra crítica em ambos os prompts: o texto gerado aqui vai direto pro TTS e é
# falado palavra por palavra. Qualquer rubrica de roteiro ("(pausa dramática)",
# "*ri*", "[risos]") é lida em voz alta pelo sintetizador, o que soa horrível
# ao vivo — por isso deixamos essa proibição bem explícita, repetida e no início.
REGRA_SAIDA_PARA_VOZ = (
    "REGRA MAIS IMPORTANTE: sua resposta vai ser lida em voz alta por um sintetizador de voz (TTS), "
    "palavra por palavra, sem nenhum tipo de edição. Por isso, escreva SOMENTE o texto que deve ser "
    "falado. NUNCA inclua rubricas de roteiro, indicações de cena, ações ou sons entre parênteses, "
    "colchetes ou asteriscos — coisas como '(pausa dramática)', '*ri*', '[risos]', '(sussurra)' são "
    "proibidas, porque o TTS vai ler isso tudo em voz alta pro público, o que fica muito estranho. "
    "Se quiser dar ênfase ou fazer uma pausa, faça isso só com as palavras e a pontuação (reticências, "
    "exclamação), nunca descrevendo a ação."
)

SYSTEM_PROMPT = (
    REGRA_SAIDA_PARA_VOZ + " "
    "Você é o assistente de voz de uma live de bate-papo descontraída no TikTok. "
    "Responda às perguntas do chat de forma curta (no máximo 2 a 3 frases), "
    "leve, simpática e com bom humor, em português do Brasil. "
    "Nunca use palavrão, conteúdo sexual, ofensivo ou pesado. "
    "Se a pergunta for confusa ou incompleta, responda de forma engraçada e gentil pedindo pra repetir. "
    "Fale como se estivesse conversando de verdade com a pessoa, sem enrolação. "
    "Se você não souber a resposta ou não tiver certeza de algo, diga isso de forma direta e simpática, "
    "sem fingir que vai checar um site e sem simular que está pesquisando em tempo real."
)

GIFT_SYSTEM_PROMPT = (
    REGRA_SAIDA_PARA_VOZ + " "
    "Você é o assistente de voz de uma live de bate-papo descontraída no TikTok. "
    "Alguém acabou de te mandar um presente virtual durante a live. "
    "Agradeça de forma curta (1 frase, no máximo 2), animada e criativa, mencionando o nome da "
    "pessoa e, se fizer sentido, o nome do presente. Varie as frases, não repita sempre o mesmo agradecimento. "
    "Nunca use palavrão, conteúdo sexual, ofensivo ou pesado. Fale em português do Brasil."
)


def obter_contexto_data_hora() -> str:
    """Monta uma frase com a data/hora reais de agora (fuso de São Paulo),
    pra injetar no prompt e o modelo parar de "chutar" ou alucinar sobre
    que dia é hoje."""
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    dias_semana = [
        "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo",
    ]
    meses = [
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    ]
    dia_semana = dias_semana[agora.weekday()]
    mes = meses[agora.month - 1]
    return (
        f"Informação real e atual: hoje é {dia_semana}, {agora.day} de {mes} de {agora.year}, "
        f"e agora são {agora.strftime('%H:%M')} (horário de São Paulo/Brasil). "
        f"Use essa informação com naturalidade se alguém perguntar a data ou as horas."
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


# NOVO: regex com as principais faixas Unicode de emojis (emoticons, símbolos,
# pictogramas, bandeiras, dingbats, variation selector, zero-width joiner etc).
# Usada só na hora de gerar o áudio — o texto "normal" (JSON, fila, tela) continua
# com os emojis, só o que vai pro sintetizador de voz é que sai limpo.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # bandeiras (pares de letras regionais)
    "\U0001F300-\U0001F5FF"  # símbolos e pictogramas diversos
    "\U0001F600-\U0001F64F"  # emoticons (carinhas)
    "\U0001F680-\U0001F6FF"  # transporte e mapas
    "\U0001F700-\U0001F77F"  # símbolos alquímicos
    "\U0001F780-\U0001F7FF"  # símbolos geométricos estendidos
    "\U0001F800-\U0001F8FF"  # setas suplementares
    "\U0001F900-\U0001F9FF"  # símbolos suplementares (emojis mais novos)
    "\U0001FA00-\U0001FA6F"  # símbolos de xadrez estendidos
    "\U0001FA70-\U0001FAFF"  # símbolos e pictogramas estendidos-A
    "\U00002600-\U000026FF"  # símbolos diversos (☀️☂️☕ etc)
    "\U00002700-\U000027BF"  # dingbats (✂️✅✈️ etc)
    "\U00002300-\U000023FF"  # símbolos técnicos (⏰⏳ etc)
    "\U00002B00-\U00002BFF"  # setas e símbolos diversos (⭐➡️ etc)
    "\U0001F000-\U0001F0FF"  # peças de mahjong/cartas/dominó
    "\U0000FE0F"              # variation selector (força apresentação como emoji)
    "\U0000200D"              # zero width joiner (junta emojis compostos, ex: família)
    "\U00002190-\U000021FF"  # setas
    "\U00002460-\U000024FF"  # números/letras em círculo
    "]+",
    flags=re.UNICODE,
)


def remover_emojis(texto: str) -> str:
    """Tira os emojis do texto antes de mandar pro TTS, pra ele não tentar
    'ler' o emoji em voz alta nem se confundir na pontuação/pausa por causa
    dele. O texto com emoji continua sendo usado normalmente em todo o resto
    (resposta JSON, fila do player, etc) — só o áudio fica sem eles."""
    if not texto:
        return texto
    texto_limpo = _EMOJI_PATTERN.sub("", texto)
    # depois de tirar o emoji pode sobrar espaço duplicado ou nas pontas — arruma isso
    texto_limpo = re.sub(r"[ \t]{2,}", " ", texto_limpo).strip()
    return texto_limpo


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
    texto_para_fala = remover_emojis(texto)  # NOVO: o TTS nunca vê/lê o emoji, só o texto puro

    async def _run():
        communicate = edge_tts.Communicate(texto_para_fala, voice, pitch=pitch, rate=rate)
        await communicate.save(caminho_saida)
    asyncio.run(_run())


def gerar_resposta_e_falar(system_prompt: str, prompt_usuario: str, prefixo_arquivo: str = "resposta",
                            voice: str = None, pitch: str = "+0Hz", rate: str = "+0%",
                            avatar_clip: str = AVATAR_CLIP_IDLE, model: str = "llama-3.3-70b-versatile"):
    completion = client.chat.completions.create(
        model=model,
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


def processar_pergunta(username: str, message: str) -> dict:
    """Toda a lógica de tratar uma pergunta do chat: filtro, rate limit, IA e fila de áudio.
    Usada tanto pelo endpoint /ask (webhook do TikFinity/Streamer.bot) quanto pelo
    listener direto da TikTok (NOVO) — assim a lógica não fica duplicada."""
    global _last_answer_time

    username = (username or "").strip()
    message = (message or "").strip()

    if len(message) < MIN_MESSAGE_LENGTH:
        return {"skipped": True, "reason": "message_too_short"}

    mensagem_lower = message.lower().strip()
    parece_pergunta = "?" in message
    usou_comando = mensagem_lower.startswith("!pergunta")
    parece_pedido_explicacao = any(expressao in mensagem_lower for expressao in PALAVRAS_DE_PERGUNTA)

    if not (parece_pergunta or usou_comando or parece_pedido_explicacao):
        return {"skipped": True, "reason": "not_a_question"}

    if usou_comando:
        # remove o "!pergunta" do começo, deixando só o texto da dúvida
        message = message[len("!pergunta"):].strip(" :,-")
        if len(message) < MIN_MESSAGE_LENGTH:
            return {"skipped": True, "reason": "message_too_short"}

    with _lock:
        now = time.time()
        if now - _last_answer_time < MIN_SECONDS_BETWEEN_ANSWERS:
            return {"skipped": True, "reason": "rate_limited"}
        _last_answer_time = now

    try:
        prompt_usuario = message if not username else f"{username} perguntou: {message}"

        # injeta a data/hora reais no prompt (resolve perguntas tipo "que dia é hoje")
        # e usa o groq/compound-mini, que tem busca na web embutida e aciona sozinho
        # quando a pergunta precisa de informação atual (notícia, resultado de jogo, etc.)
        system_prompt_atualizado = f"{SYSTEM_PROMPT} {obter_contexto_data_hora()}"
        resposta_texto, nome_arquivo = gerar_resposta_e_falar(
            system_prompt_atualizado, prompt_usuario, prefixo_arquivo="resposta",
            avatar_clip=AVATAR_CLIP_PERGUNTA,
            model="groq/compound-mini",  # acesso a dados reais/atuais via busca embutida
            pitch=PITCH_PRINCIPAL, rate=RATE_PRINCIPAL,  # tom mais pausado/envolvente
        )

        return {
            "skipped": False,
            "text": resposta_texto,
            "filename": nome_arquivo,
            "avatar_clip": AVATAR_CLIP_PERGUNTA,
        }

    except Exception as e:
        return {"skipped": True, "reason": "error", "detail": str(e)}


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(force=True, silent=True) or {}

    # Aceita tanto o formato do TikFinity (value1/value2) quanto o nosso formato de teste (username/message)
    username = str(data.get("value1", data.get("username", ""))).strip()
    message = str(data.get("value2", data.get("message", ""))).strip()

    resultado = processar_pergunta(username, message)

    if not resultado.get("skipped") and "filename" in resultado:
        base_url = request.host_url.rstrip("/")
        resultado["audio_url"] = f"{base_url}/static/audio/{resultado['filename']}"

    status = 500 if resultado.get("reason") == "error" else 200
    return jsonify(resultado), status


def processar_presente(username: str, gift_name: str) -> dict:
    """Toda a lógica de reagir a um presente: classificação por tier, rate limit,
    modo IA/NPC, escalada de voz fina->grave e fila de áudio. Usada pelo /gift
    (webhook) e pelo listener direto da TikTok (NOVO)."""
    global _last_gift_time

    username = (username or "alguém").strip() or "alguém"
    gift_name = (gift_name or "um presente").strip() or "um presente"

    tier = classificar_presente(gift_name)
    avatar_clip = TIER_TO_AVATAR_CLIP.get(tier, TIER_TO_AVATAR_CLIP["barato"])
    vai_usar_ia = (GIFT_MODE != "npc") or (tier == "caro")

    # Presentes caros (que usam IA) respeitam o cooldown mais longo, mesmo em modo NPC.
    # Presentes baratos/médios em modo NPC usam um cooldown bem curto, já que a fila cuida do resto.
    cooldown = MIN_SECONDS_BETWEEN_GIFTS if vai_usar_ia else MIN_SECONDS_BETWEEN_GIFTS_NPC

    if vai_usar_ia:
        with _lock:
            now = time.time()
            if now - _last_gift_time < cooldown:
                return {"skipped": True, "reason": "rate_limited"}
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
            voz, pitch_presente, rate_presente = voz_para_presente(tier)  # escalada fina->grave
            gerar_audio(
                resposta_texto,
                caminho_completo,
                voice=voz,
                pitch=pitch_presente,
                rate=rate_presente,
            )
            colocou_na_fila = enfileirar(resposta_texto, nome_arquivo, avatar_clip=avatar_clip)
            if not colocou_na_fila:
                return {"skipped": True, "reason": "queue_full"}

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

            voz, pitch_presente, rate_presente = voz_para_presente(tier)  # escalada fina->grave
            resposta_texto, nome_arquivo = gerar_resposta_e_falar(
                GIFT_SYSTEM_PROMPT,
                prompt_usuario,
                prefixo_arquivo="presente",
                voice=voz,
                pitch=pitch_presente,
                rate=rate_presente,
                avatar_clip=avatar_clip,
            )

        return {
            "skipped": False,
            "mode": GIFT_MODE,
            "text": resposta_texto,
            "filename": nome_arquivo,
            "tier": tier,
            "avatar_clip": avatar_clip,
        }

    except Exception as e:
        return {"skipped": True, "reason": "error", "detail": str(e)}


@app.route("/gift", methods=["POST"])
def gift():
    data = request.get_json(force=True, silent=True) or {}

    # value1 = username, value3 = nome do presente (SKU), conforme o webhook do TikFinity
    username = str(data.get("value1", data.get("username", "alguém"))).strip() or "alguém"
    gift_name = str(data.get("value3", data.get("gift", ""))).strip() or "um presente"

    resultado = processar_presente(username, gift_name)

    if not resultado.get("skipped") and "filename" in resultado:
        base_url = request.host_url.rstrip("/")
        resultado["audio_url"] = f"{base_url}/static/audio/{resultado['filename']}"

    status = 500 if resultado.get("reason") == "error" else 200
    return jsonify(resultado), status


@app.route("/falar", methods=["POST"])
def falar():
    """Rota pra fala livre 'sob demanda' (boas-vindas ou qualquer outro texto
    que você queira que o avatar diga na hora). Diferente de /ask e /gift,
    aqui o texto NÃO passa pela IA — ele fala exatamente o que você mandar,
    só gera o áudio (edge_tts) e coloca na fila do player."""
    data = request.get_json(force=True, silent=True) or {}

    # Aceita "texto" (nosso formato) ou "value2" (se um dia quiser disparar
    # via TikFinity/outro webhook usando o mesmo formato de /ask)
    texto = str(data.get("texto", data.get("value2", ""))).strip()

    if len(texto) < 1:
        return jsonify({"skipped": True, "reason": "texto_vazio"}), 200

    # Voz/pitch/rate são opcionais: se não vierem, usa a voz "principal"
    # (a mesma tonalidade pausada/envolvente do /ask)
    voice = str(data.get("voice") or VOICE_NAME).strip()
    pitch = str(data.get("pitch") or PITCH_PRINCIPAL).strip()
    rate = str(data.get("rate") or RATE_PRINCIPAL).strip()

    # Clipe do avatar exibido enquanto fala — por padrão usa o de "pergunta"
    # (boca mexendo normal); dá pra mandar outro clipe se quiser (ex: um
    # clipe específico de boas-vindas, se você criar um)
    avatar_clip = str(data.get("avatar_clip") or AVATAR_CLIP_PERGUNTA).strip()

    try:
        nome_arquivo = f"falar_{int(time.time() * 1000)}.mp3"
        caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
        gerar_audio(texto, caminho_completo, voice=voice, pitch=pitch, rate=rate)

        colocou_na_fila = enfileirar(texto, nome_arquivo, avatar_clip=avatar_clip)
        if not colocou_na_fila:
            return jsonify({"skipped": True, "reason": "queue_full"}), 200

        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/static/audio/{nome_arquivo}"

        return jsonify({
            "skipped": False,
            "text": texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
            "avatar_clip": avatar_clip,
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


# NOVO: rota pra testar rapidamente qualquer combinação de voz/pitch/rate e comparar
# o quanto cada uma soa "robótica" ou natural, sem precisar mexer em código toda hora.
# Ex: /testar-voz?voice=pt-BR-ThalitaMultilingualNeural&pitch=+0Hz&rate=+0%
VOZES_PT_BR_PARA_TESTAR = [
    "pt-BR-AntonioNeural",      # voz atual (padrão do script)
    "pt-BR-FranciscaNeural",
    "pt-BR-ThalitaMultilingualNeural",  # geração mais nova, geralmente mais natural
    "pt-BR-DonatoNeural",
    "pt-BR-FabioNeural",
    "pt-BR-JulioNeural",
    "pt-BR-NicolauNeural",
    "pt-BR-ValerioNeural",
    "pt-BR-LeticiaNeural",
    "pt-BR-BrendaNeural",
    "pt-BR-ElzaNeural",
    "pt-BR-ManuelaNeural",
    "pt-BR-GiovannaNeural",
    "pt-BR-LeilaNeural",
    "pt-BR-YaraNeural",
    "pt-BR-HumbertoNeural",
]


@app.route("/testar-voz")
def testar_voz():
    voice = request.args.get("voice", VOICE_NAME)
    pitch = request.args.get("pitch", "+0Hz")
    rate = request.args.get("rate", "+0%")
    texto = request.args.get(
        "texto",
        "Oi gente, tudo bem com vocês? Que bom ter vocês aqui na live hoje, "
        "vamos bater um papo bem gostoso!",
    )
    formato = request.args.get("formato", "html")  # NOVO: "json" pra uso via fetch() no teste.html

    nome_arquivo = f"teste_voz_{int(time.time() * 1000)}.mp3"
    caminho_completo = os.path.join(AUDIO_DIR, nome_arquivo)
    gerar_audio(texto, caminho_completo, voice=voice, pitch=pitch, rate=rate)

    base_url = request.host_url.rstrip("/")
    audio_url = f"{base_url}/static/audio/{nome_arquivo}"

    # NOVO: modo JSON, usado pela ferramenta de teste (teste.html) pra tocar
    # o áudio inline na própria página, sem precisar abrir essa rota numa aba nova
    if formato == "json":
        return jsonify({
            "voice": voice,
            "pitch": pitch,
            "rate": rate,
            "texto": texto,
            "audio_url": audio_url,
            "filename": nome_arquivo,
        })

    links_vozes = "".join(
        f'<a href="/testar-voz?voice={v}&pitch={pitch}&rate={rate}">{v}</a><br>'
        for v in VOZES_PT_BR_PARA_TESTAR
    )

    return f"""
    <html><body style="font-family:sans-serif; text-align:center; margin-top:50px;">
        <h2>Testando: {voice} (pitch {pitch}, rate {rate})</h2>
        <audio controls autoplay src="{audio_url}"></audio>
        <p style="max-width:500px; margin:20px auto; color:#555;">"{texto}"</p>
        <hr style="max-width:400px; margin:20px auto;">
        <p><b>Trocar de voz (mesmo pitch/rate):</b><br>{links_vozes}</p>
        <p style="margin-top:20px; color:#888;">
            Dica: pra testar pitch/rate diferentes, edite a URL, ex:<br>
            /testar-voz?voice={voice}&pitch=-5Hz&rate=-3%
        </p>
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


# ---------------------------------------------------------------------------
# NOVO: listener direto da TikTok — conecta no chat/presentes da sua live
# rodando aqui no Render, sem precisar de TikFinity ou Streamer.bot no seu PC.
#
# Usa a lib "TikTokLive" (não-oficial, engenharia reversa do protocolo interno
# da TikTok). Requer só o seu @usuario, sem login/senha. Configure a variável
# de ambiente TIKTOK_USERNAME no Render com o seu @ (sem o @).
#
# Opcional: TIKTOK_SIGN_API_KEY — chave gratuita da Euler Stream, serviço usado
# por baixo dos panos pra "assinar" a conexão. Ajuda a evitar instabilidade em
# uso mais pesado/contínuo. Sem ela, funciona no nível gratuito padrão.
# ---------------------------------------------------------------------------

def _iniciar_listener_tiktok():
    if not TIKTOK_USERNAME:
        print("[tiktok] TIKTOK_USERNAME não configurado — listener direto não vai iniciar. "
              "Configure essa variável de ambiente no Render com o seu @ da TikTok.")
        return

    try:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.web.web_settings import WebDefaults
        from TikTokLive.events import ConnectEvent, DisconnectEvent, CommentEvent, GiftEvent
    except ImportError:
        print("[tiktok] biblioteca TikTokLive não instalada. Adicione 'TikTokLive' ao requirements.txt.")
        return

    # A chave da Euler Stream precisa ser configurada globalmente ANTES de criar
    # o cliente — é assim que a lib TikTokLive espera receber essa configuração.
    if TIKTOK_SIGN_API_KEY:
        WebDefaults.tiktok_sign_api_key = TIKTOK_SIGN_API_KEY

    def _rodar_cliente():
        while True:
            try:
                client = TikTokLiveClient(unique_id=f"@{TIKTOK_USERNAME}")

                @client.on(ConnectEvent)
                async def _on_connect(_event):
                    print(f"[tiktok] conectado à live de @{TIKTOK_USERNAME}")

                @client.on(DisconnectEvent)
                async def _on_disconnect(_event):
                    print("[tiktok] desconectado da live")

                @client.on(CommentEvent)
                async def _on_comment(event):
                    username = event.user.nickname or event.user.unique_id
                    processar_pergunta(username, event.comment)

                @client.on(GiftEvent)
                async def _on_gift(event):
                    # presentes "em combo" (ex: mandar 10 rosas seguidas) só devem
                    # ser processados quando o combo termina, senão processa cada
                    # unidade do combo separadamente
                    if event.gift.streakable and event.streaking:
                        return
                    username = event.user.nickname or event.user.unique_id
                    processar_presente(username, event.gift.name)

                client.run()  # bloqueia essa thread até cair a conexão

            except Exception as e:
                print(f"[tiktok] listener caiu ({e}); tentando reconectar em 15s...")
                time.sleep(15)

    thread = threading.Thread(target=_rodar_cliente, daemon=True)
    thread.start()


if INICIAR_LISTENER_TIKTOK:
    _iniciar_listener_tiktok()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
