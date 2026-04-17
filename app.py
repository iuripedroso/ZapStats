from flask import Flask, render_template, request, jsonify
import zipfile
import re
import io
import base64
from collections import Counter
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB


_last_messages = {'p1': None, 'p2': None, 'msgs': []}

PATTERNS = [
    (r'\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}:\d{2})\]\s*([^:]+?):\s*(.*)', '%d/%m/%Y %H:%M:%S'),
    (r'\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2})\]\s*([^:]+?):\s*(.*)', '%d/%m/%Y %H:%M'),
    (r'(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2})\s*-\s*([^:]+?):\s*(.*)', '%d/%m/%Y %H:%M'),
    (r'(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2})\s*-\s*([^:]+?):\s*(.*)', '%d/%m/%Y %H:%M'),
    (r'(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}\s*[APap][Mm])\s*-\s*([^:]+?):\s*(.*)', None),
    (r'(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}:\d{2})\s*-\s*([^:]+?):\s*(.*)', '%d/%m/%Y %H:%M:%S'),
]

def try_parse_date(date_str, time_str, fmt):
    try:
        parts = date_str.split('/')
        if len(parts) == 3 and len(parts[2]) == 2:
            parts[2] = '20' + parts[2]
            date_str = '/'.join(parts)
        if fmt:
            return datetime.strptime(f"{date_str} {time_str}", fmt)
        else:
            return datetime.strptime(f"{date_str} {time_str.strip()}", '%m/%d/%Y %I:%M %p')
    except:
        return None

def parse_whatsapp_chat(text):
    text = text.lstrip('\ufeff')
    messages = []
    lines = text.splitlines()
    current_msg = None
    matched_pattern = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        hit = False
        check_patterns = [matched_pattern] if matched_pattern else PATTERNS
        if matched_pattern:
            check_patterns = [matched_pattern] + [p for p in PATTERNS if p != matched_pattern]

        for pat, fmt in check_patterns:
            m = re.match(pat, line)
            if m:
                if current_msg:
                    messages.append(current_msg)
                date_str, time_str, sender, content = (
                    m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()
                )
                dt = try_parse_date(date_str, time_str, fmt)
                current_msg = {'date': dt, 'sender': sender, 'content': content}
                matched_pattern = (pat, fmt)
                hit = True
                break

        if not hit and current_msg:
            current_msg['content'] += '\n' + line

    if current_msg:
        messages.append(current_msg)
    return messages

def is_media(content):
    terms = [
        'figurinha omitida', 'sticker omitted', '<figurinha', '<sticker',
        'imagem omitida', 'image omitted', '<imagem', '<image',
        'vídeo omitido', 'video omitted', '<vídeo', '<video',
        'áudio omitido', 'audio omitted', '<áudio', '<audio',
        'arquivo omitido', 'file omitted', '<arquivo', '<file',
        'mídia omitida', 'media omitted',
        'arquivo anexado', 'file attached',
    ]
    cl = content.lower()
    if any(t in cl for t in terms):
        return True
    # Catch media filenames exported with media (e.g. "STK-xxx.webp (arquivo anexado)")
    if re.search(r'\.(webp|opus|mp4|mp3|jpg|jpeg|png|gif|stk|aac|m4a|3gp|caf)\b', cl):
        return True
    return False

def is_sticker(content):
    cl = content.lower()
    return ('figurinha omitida' in cl or 'sticker omitted' in cl or
            '<figurinha' in cl or '<sticker' in cl)

def extract_sticker_filename(content):
    # With media export, WhatsApp puts the filename before "(arquivo anexado)" or similar
    # Patterns: "STK-20240101-WA0001.webp (arquivo anexado)"  or just the filename
    m = re.search(r'(STK[\w\-]+\.webp)', content, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'([\w\-]+\.webp)', content, re.IGNORECASE)
    return m.group(1) if m else None

STOPWORDS = {
    'que', 'não', 'nao', 'uma', 'com', 'por', 'para', 'como', 'mas', 'mais',
    'você', 'voce', 'isso', 'esse', 'essa', 'ele', 'ela', 'tem', 'era', 'são',
    'foi', 'ser', 'ter', 'vai', 'vou', 'sim', 'meu', 'minha', 'seu', 'sua',
    'the', 'and', 'for', 'aqui', 'ali', 'agora', 'então', 'entao', 'tudo',
    'muito', 'também', 'tambem', 'quando', 'porque', 'nada', 'assim', 'ainda',
    'bem', 'bom', 'boa', 'lá', 'pro', 'pra', 'num', 'numa', 'dos', 'das',
    'nos', 'nas', 'pelo', 'pela', 'pelos', 'pelas', 'esses', 'essas',
    'este', 'esta', 'mesmo', 'mesma', 'outros', 'outras', 'cada',
    'todo', 'toda', 'todos', 'todas', 'tipo', 'coisa', 'ver', 'faz', 'fica',
    'só', 'ate', 'até', 'aí', 'ai', 'já', 'ja', 'né', 'ne', 'acho', 'sei',
    'haha', 'kkk', 'kk', 'rsrs', 'hahaha', 'kkkkk', 'kkkk', 'ahahah',
    'omg', 'omitida', 'omitted', 'anexado', 'apagada', 'apagado',
    'mensagem', 'message', 'this', 'with', 'null', 'audio', 'video',
    'mídia', 'media', 'arquivo', 'file', 'imagem', 'image',
}

def get_top_words(messages, sender):
    words = []
    for m in messages:
        if m['sender'] == sender and not is_media(m['content']):
            text = m['content'].lower()
            text = re.sub(r'https?://\S+', '', text)
            text = re.sub(r'[^\w\sáàãâéêíóõôúüçñ]', ' ', text, flags=re.UNICODE)
            ws = [w for w in text.split() if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]
            words.extend(ws)
    return Counter(words).most_common(5)

def count_phrase(messages, sender, phrase):
    """Count how many messages contain the phrase (case-insensitive)."""
    phrase = phrase.lower().strip()
    count = 0
    for m in messages:
        if m['sender'] == sender and not is_media(m['content']):
            if phrase in m['content'].lower():
                count += 1
    return count

def get_top_stickers(messages, sender, media_files):
    sticker_counts = Counter()
    for m in messages:
        if m['sender'] == sender and is_sticker(m['content']):
            fname = extract_sticker_filename(m['content'])
            key = fname if fname else '__unknown__'
            sticker_counts[key] += 1

    total = sum(sticker_counts.values())
    top = sticker_counts.most_common(3)

    result = []
    for fname, count in top:
        entry = {'count': count, 'img': None}
        if fname != '__unknown__':
            # Try to find the file - match by name ignoring path
            if fname in media_files:
                entry['img'] = 'data:image/webp;base64,' + base64.b64encode(media_files[fname]).decode()
        result.append(entry)

    return result, total

def analyze_chat(messages, media_files):
    if not messages:
        return {'error': 'Nenhuma mensagem encontrada no arquivo.'}

    system_keywords = [
        'cifrado de ponta', 'end-to-end', 'security code', 'as mensagens',
        'messages to this', 'changed their', 'added', 'removed', 'left',
        'criou o grupo', 'created group', 'adicionou', 'saiu', 'removeu',
        'null', 'apagou esta mensagem', 'you deleted this', 'this message was deleted'
    ]
    real = [m for m in messages if not any(kw in m['content'].lower() for kw in system_keywords)]

    if not real:
        return {'error': 'Só encontrei mensagens de sistema. Verifique se exportou a conversa corretamente.'}

    sender_counts = Counter(m['sender'] for m in real)
    top2 = sender_counts.most_common(2)

    if len(top2) < 2:
        return {'error': f'Encontrei apenas 1 participante ({top2[0][0]}). Preciso de uma conversa entre 2 pessoas.'}

    p1_name, p1_msgs = top2[0]
    p2_name, p2_msgs = top2[1]
    msgs_filtered = [m for m in real if m['sender'] in [p1_name, p2_name]]

    # Store for word search
    _last_messages['p1'] = p1_name
    _last_messages['p2'] = p2_name
    _last_messages['msgs'] = msgs_filtered

    # Streaks
    dates = sorted(set(m['date'].date() for m in msgs_filtered if m['date']))
    streaks = []
    if dates:
        start = dates[0]; length = 1
        for i in range(1, len(dates)):
            if (dates[i] - dates[i-1]).days == 1:
                length += 1
            else:
                streaks.append((start, dates[i-1], length))
                start = dates[i]; length = 1
        streaks.append((start, dates[-1], length))
    longest = max(streaks, key=lambda x: x[2]) if streaks else None

    has_media = len(media_files) > 0
    p1_top_stickers, p1_sticker_total = get_top_stickers(msgs_filtered, p1_name, media_files)
    p2_top_stickers, p2_sticker_total = get_top_stickers(msgs_filtered, p2_name, media_files)

    return {
        'has_media': has_media,
        'person1': {
            'name': p1_name, 'msgs': p1_msgs,
            'top_word': get_top_words(msgs_filtered, p1_name),
            'stickers': p1_sticker_total,
            'top_stickers': p1_top_stickers,
        },
        'person2': {
            'name': p2_name, 'msgs': p2_msgs,
            'top_word': get_top_words(msgs_filtered, p2_name),
            'stickers': p2_sticker_total,
            'top_stickers': p2_top_stickers,
        },
        'total_msgs': p1_msgs + p2_msgs,
        'longest_streak': {'days': longest[2], 'start': str(longest[0]), 'end': str(longest[1])} if longest else None,
        'total_days': len(dates),
        'first_date': str(dates[0]) if dates else None,
        'last_date': str(dates[-1]) if dates else None,
        'streaks_count': len(streaks),
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    phrase = (data or {}).get('phrase', '').strip()
    if not phrase:
        return jsonify({'error': 'Digite uma palavra ou frase'}), 400
    if not _last_messages['msgs']:
        return jsonify({'error': 'Faça upload de uma conversa primeiro'}), 400

    msgs = _last_messages['msgs']
    p1 = _last_messages['p1']
    p2 = _last_messages['p2']

    p1_count = count_phrase(msgs, p1, phrase)
    p2_count = count_phrase(msgs, p2, phrase)

    return jsonify({
        'phrase': phrase,
        'p1_name': p1, 'p1_count': p1_count,
        'p2_name': p2, 'p2_count': p2_count,
        'total': p1_count + p2_count,
    })

@app.route('/analyze', methods=['POST'])
def analyze():
    if 'file' not in request.files:
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Por favor, envie um arquivo .zip exportado do WhatsApp'}), 400

    try:
        zip_data = file.read()
        content = None
        media_files = {}

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            all_files = z.namelist()
            txt_files = [f for f in all_files if f.lower().endswith('.txt')]

            if not txt_files:
                return jsonify({
                    'error': f'Nenhum .txt encontrado. Arquivos: {", ".join(all_files[:5])}'
                }), 400

            for tf in txt_files:
                raw = z.open(tf).read()
                for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
                    try:
                        content = raw.decode(enc)
                        break
                    except:
                        continue
                if content:
                    break

            for fname in all_files:
                if fname.lower().endswith('.webp'):
                    try:
                        data = z.open(fname).read()
                        if len(data) <= 5 * 1024 * 1024:
                            media_files[fname.split('/')[-1]] = data
                    except:
                        pass

        if not content:
            return jsonify({'error': 'Não consegui ler o arquivo de texto do zip.'}), 400

        messages = parse_whatsapp_chat(content)
        if not messages:
            sample = '\n'.join(content.splitlines()[:3])
            return jsonify({'error': f'Não reconheci o formato. Primeiras linhas: {sample}'}), 400

        result = analyze_chat(messages, media_files)
        if 'error' in result:
            return jsonify(result), 400

        return jsonify(result)

    except zipfile.BadZipFile:
        return jsonify({'error': 'Arquivo zip inválido ou corrompido'}), 400
    except Exception as e:
        import traceback
        return jsonify({'error': f'Erro interno: {str(e)}', 'trace': traceback.format_exc()}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'Arquivo muito grande. Tente exportar sem mídia.'}), 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
