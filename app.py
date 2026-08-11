from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import qrcode
import io
import base64
import os
import secrets
import string
import re
from functools import wraps

# ─── TIMEZONE BRASIL ──────────────────────────────────────────────────────────

BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")

def agora_brasilia():
    return datetime.now(BRASILIA_TZ)

# ─── APP CONFIG ───────────────────────────────────────────────────────────────

app = Flask(__name__)

secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "Variável de ambiente SECRET_KEY não definida. "
        "Defina no Railway antes de iniciar a aplicação."
    )
app.config['SECRET_KEY'] = secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.getenv("FLASK_ENV", "production") == "production"
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD", "")
    db_host = os.getenv("DB_HOST", "localhost")
    db_name = os.getenv("DB_NAME", "catecontrol")
    app.config['SQLALCHEMY_DATABASE_URI'] = f"postgresql://{db_user}:{db_pass}@{db_host}/{db_name}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ─── LÓGICA DE IDADE ──────────────────────────────────────────────────────────

def calcular_tipo_por_idade(data_nascimento_str: str) -> str:
    if not data_nascimento_str:
        return 'catequizando'
    try:
        nascimento = datetime.strptime(data_nascimento_str, '%Y-%m-%d').date()
        hoje = date.today()
        idade = (
            hoje.year - nascimento.year
            - ((hoje.month, hoje.day) < (nascimento.month, nascimento.day))
        )
        return 'adulto' if idade >= 18 else 'catequizando'
    except (ValueError, TypeError):
        return 'catequizando'

# ─── MODELS ───────────────────────────────────────────────────────────────────

class CatequistaPatio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False, unique=True)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)

    def to_dict(self):
        return {'id': self.id, 'nome': self.nome, 'ativo': self.ativo}


class MonitorAutorizador(db.Model):
    """
    Pessoas com 'acesso tipo administrador' no leitor, usadas para liberação
    excepcional de saída de menores quando o responsável cadastrado não está
    presente (ex: catequista/monitor de plantão). Autenticam por nome + PIN.
    """
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False, unique=True)
    pin_hash = db.Column(db.String(200), nullable=False)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)

    def set_pin(self, pin: str):
        from werkzeug.security import generate_password_hash
        self.pin_hash = generate_password_hash(pin, method='pbkdf2:sha256', salt_length=16)

    def check_pin(self, pin: str) -> bool:
        from werkzeug.security import check_password_hash
        return check_password_hash(self.pin_hash, pin)

    def to_dict(self):
        return {'id': self.id, 'nome': self.nome, 'ativo': self.ativo}


class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(15), nullable=False)
    responsavel_codigo = db.Column(db.String(20), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    data_nascimento = db.Column(db.String(20), nullable=True)
    turma = db.Column(db.String(80), nullable=True)
    catequista_patio_id = db.Column(db.Integer, db.ForeignKey('catequista_patio.id'), nullable=True)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)
    
    # NOVO CAMPO: Foto
    foto = db.Column(db.Text, nullable=True)

    catequista_patio = db.relationship('CatequistaPatio', backref='catequizandos', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nome': self.nome,
            'tipo': self.tipo,
            'responsavel_codigo': self.responsavel_codigo,
            'telefone': self.telefone,
            'email': self.email,
            'data_nascimento': self.data_nascimento,
            'turma': self.turma,
            'catequista_patio_id': self.catequista_patio_id,
            'catequista_patio_nome': self.catequista_patio.nome if self.catequista_patio else None,
            'ativo': self.ativo,
            'foto': self.foto  # Adicionado ao dicionário
        }


class Registro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pessoa_codigo = db.Column(db.String(20), nullable=False)
    pessoa_nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    horario = db.Column(db.DateTime(timezone=True), default=agora_brasilia)
    autorizado_por = db.Column(db.String(120), nullable=True)

    def to_dict(self):
        h = self.horario
        if h.tzinfo is None:
            h = h.replace(tzinfo=BRASILIA_TZ)
        else:
            h = h.astimezone(BRASILIA_TZ)
        return {
            'id': self.id,
            'pessoa_codigo': self.pessoa_codigo,
            'pessoa_nome': self.pessoa_nome,
            'tipo': self.tipo,
            'horario': h.strftime('%d/%m/%Y %H:%M:%S'),
            'autorizado_por': self.autorizado_por
        }


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, password: str):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)

    def check_password(self, password: str) -> bool:
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)


with app.app_context():
    db.create_all()
    if not Admin.query.filter_by(username="admin").first():
        senha_inicial = os.getenv("ADMIN_INITIAL_PASSWORD")
        if not senha_inicial:
            senha_inicial = secrets.token_hex(16)
            print(
                "AVISO: Variável de ambiente ADMIN_INITIAL_PASSWORD não definida. "
                f"Uma senha temporária foi gerada para o usuário 'admin': {senha_inicial} "
                "Defina ADMIN_INITIAL_PASSWORD no Railway e altere a senha após o primeiro login."
            )
        novo_admin = Admin(username="admin")
        novo_admin.set_password(senha_inicial)
        db.session.add(novo_admin)
        db.session.commit()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_logged_in' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Não autenticado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def gerar_codigo(tipo='catequizando'):
    prefixos = {'catequizando': 'CA', 'adulto': 'AD', 'responsavel': 'RS'}
    prefixo = prefixos.get(tipo, 'CA')
    ano = datetime.now().year
    charset = string.ascii_uppercase + string.digits
    while True:
        aleatorio = ''.join(secrets.choice(charset) for _ in range(8))
        codigo = f'{prefixo}-{ano}-{aleatorio}'
        if not Pessoa.query.filter_by(codigo=codigo).first():
            return codigo

def pode_registrar(codigo):
    trinta_seg_atras = agora_brasilia() - timedelta(seconds=30)
    return Registro.query.filter(
        Registro.pessoa_codigo == codigo,
        Registro.horario > trinta_seg_atras
    ).first() is None

def calcular_carga_horaria_segundos(registros):
    """
    Recebe uma lista de Registro (qualquer ordem) e soma o tempo entre cada
    par entrada -> saída consecutivo. Entradas sem saída correspondente
    (pessoa ainda presente, ou saída não registrada) não são contabilizadas.
    """
    total = timedelta()
    entrada_pendente = None
    for r in sorted(registros, key=lambda x: x.horario):
        if r.tipo == 'entrada':
            entrada_pendente = r.horario
        elif r.tipo == 'saida' and entrada_pendente is not None:
            total += (r.horario - entrada_pendente)
            entrada_pendente = None
    return int(total.total_seconds())

def formatar_carga_horaria(segundos: int) -> str:
    horas = segundos // 3600
    minutos = (segundos % 3600) // 60
    return f'{horas}h {minutos:02d}min'

def gerar_qr_base64(codigo):
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(codigo)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#3B1FA8', back_color='white')
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()

def normalizar_nome(nome: str) -> str:
    return re.sub(r'\s+', ' ', nome.strip()).upper()

# ─── ROTAS PÚBLICAS ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('leitor.html')

@app.route('/api/registrar', methods=['POST'])
def registrar():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Requisição inválida.'}), 400

    codigo = (data.get('codigo') or '').strip().upper()
    tipo_extra = (data.get('tipo_extra') or '').strip().upper() or None

    if not codigo:
        return jsonify({'success': False, 'message': 'Código não informado.'}), 400

    if len(codigo) > 30:
        return jsonify({'success': False, 'message': 'Código inválido.'}), 400

    pessoa = Pessoa.query.filter_by(codigo=codigo, ativo=True).first()
    if not pessoa:
        return jsonify({'success': False, 'message': 'QR Code não reconhecido ou pessoa inativa.'}), 404

    ultimo = Registro.query.filter_by(pessoa_codigo=codigo).order_by(Registro.horario.desc()).first()
    tipo_registro = 'entrada' if (ultimo is None or ultimo.tipo == 'saida') else 'saida'

    if not pode_registrar(codigo):
        return jsonify({'success': False, 'message': 'Aguarde 30 segundos para registrar novamente.'}), 429

    if tipo_registro == 'saida' and pessoa.tipo == 'catequizando' and pessoa.responsavel_codigo:
        if not tipo_extra:
            return jsonify({
                'success': False,
                'requer_responsavel': True,
                'message': f'Apresente o QR Code do responsável para autorizar a saída de {pessoa.nome}.',
                'pessoa': pessoa.to_dict()
            }), 200

        responsavel = Pessoa.query.filter_by(codigo=tipo_extra, ativo=True).first()
        if not responsavel or responsavel.codigo != pessoa.responsavel_codigo:
            return jsonify({'success': False, 'message': 'QR Code do responsável inválido ou não corresponde.'}), 403

        registro = Registro(
            pessoa_codigo=codigo,
            pessoa_nome=pessoa.nome,
            tipo='saida',
            autorizado_por=responsavel.nome
        )
        db.session.add(registro)
        db.session.commit()
        h = registro.horario
        if h.tzinfo is None:
            h = h.replace(tzinfo=BRASILIA_TZ)
        else:
            h = h.astimezone(BRASILIA_TZ)
        return jsonify({
            'success': True, 'tipo': 'saida',
            'pessoa': pessoa.to_dict(),
            'horario': h.strftime('%H:%M:%S'),
            'message': f'Saída de {pessoa.nome} autorizada por {responsavel.nome}.'
        })

    registro = Registro(pessoa_codigo=codigo, pessoa_nome=pessoa.nome, tipo=tipo_registro)
    db.session.add(registro)
    db.session.commit()
    h = registro.horario
    if h.tzinfo is None:
        h = h.replace(tzinfo=BRASILIA_TZ)
    else:
        h = h.astimezone(BRASILIA_TZ)
    return jsonify({
        'success': True, 'tipo': tipo_registro,
        'pessoa': pessoa.to_dict(),
        'horario': h.strftime('%H:%M:%S'),
        'message': f'{"Entrada" if tipo_registro == "entrada" else "Saída"} de {pessoa.nome} registrada!'
    })

@app.route('/api/atividade_recente')
def atividade_recente():
    registros = Registro.query.order_by(Registro.horario.desc()).limit(10).all()
    return jsonify([r.to_dict() for r in registros])

@app.route('/api/monitores_ativos')
def api_monitores_ativos():
    """Lista pública (sem PIN) para popular o seletor no leitor."""
    monitores = MonitorAutorizador.query.filter_by(ativo=True).order_by(MonitorAutorizador.nome).all()
    return jsonify([{'id': m.id, 'nome': m.nome} for m in monitores])

@app.route('/api/registrar_excecao', methods=['POST'])
def registrar_excecao():
    """
    Liberação excepcional de saída de um catequizando por um monitor
    autorizado (acesso tipo administrador), usada quando o responsável
    cadastrado não está presente para liberar a criança.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Requisição inválida.'}), 400

    codigo = (data.get('codigo') or '').strip().upper()
    monitor_id = data.get('monitor_id')
    pin = (data.get('pin') or '').strip()

    if not codigo or not monitor_id or not pin:
        return jsonify({'success': False, 'message': 'Preencha o monitor e o PIN.'}), 400

    pessoa = Pessoa.query.filter_by(codigo=codigo, ativo=True).first()
    if not pessoa:
        return jsonify({'success': False, 'message': 'QR Code não reconhecido ou pessoa inativa.'}), 404

    monitor = MonitorAutorizador.query.filter_by(id=monitor_id, ativo=True).first()
    if not monitor or not monitor.check_pin(pin):
        return jsonify({'success': False, 'message': 'Monitor ou PIN inválido.'}), 403

    ultimo = Registro.query.filter_by(pessoa_codigo=codigo).order_by(Registro.horario.desc()).first()
    tipo_registro = 'entrada' if (ultimo is None or ultimo.tipo == 'saida') else 'saida'

    if tipo_registro != 'saida':
        return jsonify({'success': False, 'message': 'Esta pessoa não está com saída pendente.'}), 400

    if not pode_registrar(codigo):
        return jsonify({'success': False, 'message': 'Aguarde 30 segundos para registrar novamente.'}), 429

    registro = Registro(
        pessoa_codigo=codigo,
        pessoa_nome=pessoa.nome,
        tipo='saida',
        autorizado_por=f'{monitor.nome} (Liberação excepcional)'
    )
    db.session.add(registro)
    db.session.commit()
    h = registro.horario
    if h.tzinfo is None:
        h = h.replace(tzinfo=BRASILIA_TZ)
    else:
        h = h.astimezone(BRASILIA_TZ)
    return jsonify({
        'success': True, 'tipo': 'saida',
        'pessoa': pessoa.to_dict(),
        'horario': h.strftime('%H:%M:%S'),
        'autorizado_por': f'{monitor.nome} (Liberação excepcional)',
        'message': f'Saída de {pessoa.nome} liberada excepcionalmente por {monitor.nome}.'
    })

# ─── ROTAS ADMIN ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'Requisição inválida'}), 400
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if len(username) > 80 or len(password) > 200:
            return jsonify({'success': False, 'message': 'Credenciais inválidas'}), 401

        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            session.permanent = True
            session['admin_logged_in'] = True
            session['admin_username'] = admin.username
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Credenciais inválidas'}), 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/admin/pessoas')
@login_required
def pessoas():
    return render_template('pessoas.html')

@app.route('/admin/cadastrar')
@login_required
def cadastrar():
    return render_template('cadastrar.html')

@app.route('/admin/relatorios')
@login_required
def relatorios():
    return render_template('relatorios.html')

# ─── API ADMIN ────────────────────────────────────────────────────────────────

@app.route('/api/admin/dashboard')
@login_required
def api_dashboard():
    hoje = agora_brasilia().date()
    inicio_hoje = datetime.combine(hoje, datetime.min.time()).replace(tzinfo=BRASILIA_TZ)
    fim_hoje = datetime.combine(hoje, datetime.max.time()).replace(tzinfo=BRASILIA_TZ)

    entradas_hoje = Registro.query.filter(Registro.tipo == 'entrada', Registro.horario.between(inicio_hoje, fim_hoje)).count()
    saidas_hoje = Registro.query.filter(Registro.tipo == 'saida', Registro.horario.between(inicio_hoje, fim_hoje)).count()
    presentes = entradas_hoje - saidas_hoje
    cadastrados = Pessoa.query.filter_by(ativo=True).count()

    frequencia = []
    for i in range(6, -1, -1):
        dia = agora_brasilia().date() - timedelta(days=i)
        inicio = datetime.combine(dia, datetime.min.time()).replace(tzinfo=BRASILIA_TZ)
        fim = datetime.combine(dia, datetime.max.time()).replace(tzinfo=BRASILIA_TZ)
        ent = Registro.query.filter(Registro.tipo == 'entrada', Registro.horario.between(inicio, fim)).count()
        sai = Registro.query.filter(Registro.tipo == 'saida', Registro.horario.between(inicio, fim)).count()
        frequencia.append({'dia': dia.strftime('%d/%m'), 'entradas': ent, 'saidas': sai})

    atividade = Registro.query.order_by(Registro.horario.desc()).limit(10).all()
    return jsonify({
        'entradas_hoje': entradas_hoje,
        'saidas_hoje': saidas_hoje,
        'presentes': max(0, presentes),
        'cadastrados': cadastrados,
        'frequencia': frequencia,
        'atividade': [r.to_dict() for r in atividade]
    })

@app.route('/api/admin/pessoas', methods=['GET'])
@login_required
def api_pessoas():
    q = request.args.get('q', '')[:100]
    tipo = request.args.get('tipo', '')
    query = Pessoa.query.filter_by(ativo=True)
    if tipo in ('catequizando', 'adulto', 'responsavel'):
        query = query.filter_by(tipo=tipo)
    if q:
        query = query.filter((Pessoa.nome.ilike(f'%{q}%')) | (Pessoa.codigo.ilike(f'%{q}%')))
    return jsonify([p.to_dict() for p in query.order_by(Pessoa.nome).all()])

@app.route('/api/admin/pessoas', methods=['POST'])
@login_required
def api_cadastrar_pessoa():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Dados inválidos'}), 400

    nome_raw = (data.get('nome') or '').strip()
    if not nome_raw:
        return jsonify({'success': False, 'message': 'Nome é obrigatório'}), 400

    nome = normalizar_nome(nome_raw)
    tipo_base = data.get('tipo', 'catequizando')
    
    duplicado = Pessoa.query.filter(db.func.upper(Pessoa.nome) == nome, Pessoa.ativo == True).first()
    if duplicado:
        return jsonify({'success': False, 'message': f'Já existe uma pessoa cadastrada com o nome "{nome}".'}), 409

    data_nascimento = data.get('data_nascimento') or None
    tipo_final = calcular_tipo_por_idade(data_nascimento) if tipo_base == 'catequizando' else 'responsavel'

    try:
        catequista_id = int(data.get('catequista_patio_id')) if data.get('catequista_patio_id') else None
    except (ValueError, TypeError):
        catequista_id = None

    codigo = gerar_codigo(tipo_final)

    pessoa = Pessoa(
        codigo=codigo,
        nome=nome,
        tipo=tipo_final,
        responsavel_codigo=data.get('responsavel_codigo') or None,
        telefone=(data.get('telefone') or '')[:20] or None,
        email=(data.get('email') or '')[:120] or None,
        data_nascimento=data_nascimento,
        turma=(data.get('turma') or '')[:80] or None,
        catequista_patio_id=catequista_id if tipo_base == 'catequizando' else None,
        foto=data.get('foto') or None # Adicionando a foto no cadastro
    )
    db.session.add(pessoa)
    db.session.commit()
    return jsonify({'success': True, 'pessoa': pessoa.to_dict(), 'codigo': codigo})

@app.route('/api/admin/pessoas/<int:pessoa_id>', methods=['PUT'])
@login_required
def api_atualizar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    data = request.get_json(silent=True)
    
    nome_raw = (data.get('nome') or '').strip()
    if not nome_raw:
        return jsonify({'success': False, 'message': 'Nome é obrigatório'}), 400

    pessoa.nome = normalizar_nome(nome_raw)
    pessoa.telefone = (data.get('telefone') or '')[:20] or None
    pessoa.email = (data.get('email') or '')[:120] or None
    pessoa.responsavel_codigo = data.get('responsavel_codigo') or None
    pessoa.turma = (data.get('turma') or '')[:80] or None
    pessoa.data_nascimento = data.get('data_nascimento') or None
    
    if 'foto' in data:
        pessoa.foto = data.get('foto')

    if pessoa.tipo in ('catequizando', 'adulto'):
        pessoa.tipo = calcular_tipo_por_idade(pessoa.data_nascimento)

    try:
        pessoa.catequista_patio_id = int(data.get('catequista_patio_id')) if data.get('catequista_patio_id') else None
    except (ValueError, TypeError):
        pessoa.catequista_patio_id = None

    db.session.commit()
    return jsonify({'success': True, 'pessoa': pessoa.to_dict()})

@app.route('/api/admin/pessoas/<int:pessoa_id>', methods=['DELETE'])
@login_required
def api_deletar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    pessoa.ativo = False
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/admin/qrcode/<path:codigo>')
@login_required
def api_qrcode(codigo):
    pessoa = Pessoa.query.filter_by(codigo=codigo.upper(), ativo=True).first()
    if not pessoa:
        return jsonify({'error': 'Pessoa não encontrada'}), 404
    return jsonify({'qr': gerar_qr_base64(pessoa.codigo)})

@app.route('/api/admin/relatorio/<path:codigo>')
@login_required
def api_relatorio(codigo):
    pessoa = Pessoa.query.filter_by(codigo=codigo).first()
    if not pessoa:
        return jsonify({'error': 'Pessoa não encontrada'}), 404

    data_inicio = request.args.get('inicio')
    data_fim = request.args.get('fim')
    query = Registro.query.filter_by(pessoa_codigo=codigo)

    try:
        if data_inicio:
            inicio_dt = datetime.strptime(data_inicio, '%Y-%m-%d').replace(tzinfo=BRASILIA_TZ)
            query = query.filter(Registro.horario >= inicio_dt)
        if data_fim:
            fim_dt = (datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=BRASILIA_TZ)
            query = query.filter(Registro.horario < fim_dt)
    except ValueError:
        return jsonify({'error': 'Formato de data inválido. Use YYYY-MM-DD'}), 400

    registros = query.order_by(Registro.horario.desc()).all()
    carga_segundos = calcular_carga_horaria_segundos(registros)
    return jsonify({
        'pessoa': pessoa.to_dict(),
        'registros': [r.to_dict() for r in registros],
        'total_entradas': sum(1 for r in registros if r.tipo == 'entrada'),
        'total_saidas': sum(1 for r in registros if r.tipo == 'saida'),
        'carga_horaria_segundos': carga_segundos,
        'carga_horaria_formatada': formatar_carga_horaria(carga_segundos)
    })

@app.route('/api/admin/responsaveis')
@login_required
def api_responsaveis():
    resp = Pessoa.query.filter_by(tipo='responsavel', ativo=True).order_by(Pessoa.nome).all()
    return jsonify([{'codigo': p.codigo, 'nome': p.nome} for p in resp])

@app.route('/api/admin/catequistas_patio', methods=['GET', 'POST'])
@login_required
def api_catequistas_patio():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        nome = (data.get('nome') or '').strip()
        if not nome:
            return jsonify({'success': False, 'message': 'Informe o nome.'}), 400
        existe = CatequistaPatio.query.filter(CatequistaPatio.nome.ilike(nome), CatequistaPatio.ativo == True).first()
        if existe:
            return jsonify({'success': False, 'message': 'Já existe um catequista com esse nome.'}), 409
        c = CatequistaPatio(nome=nome)
        db.session.add(c)
        db.session.commit()
        return jsonify({'success': True, 'catequista': c.to_dict()})
        
    lista = CatequistaPatio.query.filter_by(ativo=True).order_by(CatequistaPatio.nome).all()
    return jsonify([c.to_dict() for c in lista])

@app.route('/api/admin/catequistas_patio/<int:cid>', methods=['DELETE'])
@login_required
def api_deletar_catequista_patio(cid):
    c = CatequistaPatio.query.get_or_404(cid)
    c.ativo = False
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/admin/monitores', methods=['GET', 'POST'])
@login_required
def api_monitores():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        nome = (data.get('nome') or '').strip()
        pin = (data.get('pin') or '').strip()
        if not nome:
            return jsonify({'success': False, 'message': 'Informe o nome.'}), 400
        if not pin.isdigit() or not (4 <= len(pin) <= 6):
            return jsonify({'success': False, 'message': 'O PIN deve ter entre 4 e 6 dígitos numéricos.'}), 400
        existe = MonitorAutorizador.query.filter(MonitorAutorizador.nome.ilike(nome), MonitorAutorizador.ativo == True).first()
        if existe:
            return jsonify({'success': False, 'message': 'Já existe um monitor com esse nome.'}), 409
        m = MonitorAutorizador(nome=nome)
        m.set_pin(pin)
        db.session.add(m)
        db.session.commit()
        return jsonify({'success': True, 'monitor': m.to_dict()})

    lista = MonitorAutorizador.query.filter_by(ativo=True).order_by(MonitorAutorizador.nome).all()
    return jsonify([m.to_dict() for m in lista])

@app.route('/api/admin/monitores/<int:mid>', methods=['DELETE'])
@login_required
def api_deletar_monitor(mid):
    m = MonitorAutorizador.query.get_or_404(mid)
    m.ativo = False
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/admin/trocar_senha', methods=['POST'])
@login_required
def api_trocar_senha():
    data = request.get_json(silent=True)
    senha_atual = data.get('senha_atual') or ''
    nova_senha = data.get('nova_senha') or ''
    confirmar = data.get('confirmar') or ''

    if not senha_atual or not nova_senha or not confirmar:
        return jsonify({'success': False, 'message': 'Preencha todos os campos.'}), 400
    if nova_senha != confirmar:
        return jsonify({'success': False, 'message': 'Nova senha e confirmação não coincidem.'}), 400
    if len(nova_senha) < 8:
        return jsonify({'success': False, 'message': 'A nova senha deve ter pelo menos 8 caracteres.'}), 400

    admin = Admin.query.filter_by(username=session.get('admin_username')).first()
    if not admin or not admin.check_password(senha_atual):
        return jsonify({'success': False, 'message': 'Senha atual incorreta.'}), 401

    admin.set_password(nova_senha)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Senha alterada com sucesso!'})

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")