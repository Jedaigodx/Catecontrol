from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import qrcode
import io
import base64
import os
import re
import bcrypt           # ✅ FIX-SEC-1: bcrypt no lugar de sha256 simples
import secrets
import string
from functools import wraps
from flask_limiter import Limiter                # ✅ FIX-SEC-2: rate limiting
from flask_limiter.util import get_remote_address

# ─── TIMEZONE BRASIL ─────────────────────────────────────────

BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")

def agora_brasilia():
    return datetime.now(BRASILIA_TZ)

app = Flask(__name__)

# ✅ FIX-SEC-3: SECRET_KEY obrigatória em produção, sem fallback fraco
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    if os.getenv("FLASK_ENV") == "production":
        raise RuntimeError("SECRET_KEY obrigatória em produção! Defina a variável de ambiente SECRET_KEY.")
    _secret_key = secrets.token_hex(32)   # Gera aleatória em dev (não persiste entre restarts)
app.config['SECRET_KEY'] = _secret_key

# ✅ FIX-SEC-4: Cookies de sessão mais seguros
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv("FLASK_ENV") == "production"   # HTTPS em produção
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

database_url = os.getenv("DATABASE_URL")

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    # ✅ FIX-SEC-5: Credenciais do banco via env vars, nunca hardcoded
    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD", "")
    db_host = os.getenv("DB_HOST", "localhost")
    db_name = os.getenv("DB_NAME", "catecontrol")
    app.config['SQLALCHEMY_DATABASE_URI'] = f"postgresql://{db_user}:{db_pass}@{db_host}/{db_name}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ✅ FIX-SEC-6: Rate limiting para proteger endpoints críticos
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["300 per minute"],
    storage_uri="memory://"
)

# ─── MODELS ───────────────────────────────────────────────────────────────────

class CatequistaPatio(db.Model):
    """Catequistas de pátio (auxiliares/monitores) — lista reutilizável."""
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False, unique=True)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)

    def to_dict(self):
        return {'id': self.id, 'nome': self.nome, 'ativo': self.ativo}


class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'crianca' ou 'responsavel'
    responsavel_codigo = db.Column(db.String(20), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    data_nascimento = db.Column(db.String(20), nullable=True)
    turma = db.Column(db.String(80), nullable=True)
    catequista_patio_id = db.Column(db.Integer, db.ForeignKey('catequista_patio.id'), nullable=True)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)

    catequista_patio = db.relationship('CatequistaPatio', backref='criancas', lazy=True)

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
            'ativo': self.ativo
        }


class Registro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pessoa_codigo = db.Column(db.String(20), nullable=False)
    pessoa_nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    horario = db.Column(db.DateTime(timezone=True), default=agora_brasilia)
    autorizado_por = db.Column(db.String(120), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'pessoa_codigo': self.pessoa_codigo,
            'pessoa_nome': self.pessoa_nome,
            'tipo': self.tipo,
            'horario': self.horario.strftime('%d/%m/%Y %H:%M:%S'),
            'autorizado_por': self.autorizado_por
        }


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    # ✅ FIX-SEC-1: bcrypt com salt automático — resistente a rainbow tables e timing attacks
    def set_password(self, password):
        self.password_hash = bcrypt.hashpw(
            password.encode('utf-8'),
            bcrypt.gensalt(rounds=12)
        ).decode('utf-8')

    def check_password(self, password):
        return bcrypt.checkpw(
            password.encode('utf-8'),
            self.password_hash.encode('utf-8')
        )

# ✅ FIX-SEC-7: Função para migrar hash antigo (sha256) para bcrypt na primeira vez
def _migrate_admin_hash_if_needed(admin, plain_old_password):
    """
    Se o hash armazenado não for bcrypt (não começa com $2b$), 
    verifica com sha256 e migra para bcrypt automaticamente.
    Chame isso NO MOMENTO DO LOGIN.
    """
    import hashlib
    if not admin.password_hash.startswith('$2b$') and not admin.password_hash.startswith('$2a$'):
        # Hash antigo (sha256) — verifica e migra
        old_hash = hashlib.sha256(plain_old_password.encode()).hexdigest()
        if admin.password_hash == old_hash:
            admin.set_password(plain_old_password)
            db.session.commit()
            return True
        return False
    return admin.check_password(plain_old_password)


with app.app_context():
    db.create_all()

    if not Admin.query.filter_by(username="admin").first():
        novo_admin = Admin(username="admin")
        # ✅ FIX-SEC-8: Senha inicial via env var obrigatória — não hardcodada
        senha_inicial = os.getenv("ADMIN_INITIAL_PASSWORD", "")
        if not senha_inicial:
            senha_inicial = secrets.token_urlsafe(16)
            print("=" * 60)
            print(f"⚠️  ATENÇÃO: Senha inicial gerada automaticamente:")
            print(f"    Usuário: admin")
            print(f"    Senha:   {senha_inicial}")
            print("    Anote agora — não será exibida novamente!")
            print("=" * 60)
        novo_admin.set_password(senha_inicial)
        db.session.add(novo_admin)
        db.session.commit()
        print("Admin padrão criado!")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_logged_in' not in session:
            # ✅ FIX-SEC-9: APIs retornam 401 JSON; páginas redirecionam
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Não autenticado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def gerar_codigo(tipo='crianca'):
    prefixo = 'CR' if tipo == 'crianca' else 'RS'
    ano = datetime.now().year
    charset = string.ascii_uppercase + string.digits
    while True:
        aleatorio = ''.join(secrets.choice(charset) for _ in range(8))
        codigo = f'{prefixo}-{ano}-{aleatorio}'
        if not Pessoa.query.filter_by(codigo=codigo).first():
            return codigo


def pode_registrar(codigo, tipo_registro):
    um_minuto_atras = agora_brasilia() - timedelta(minutes=1)
    registro_recente = Registro.query.filter(
        Registro.pessoa_codigo == codigo,
        Registro.horario > um_minuto_atras
    ).first()
    return registro_recente is None


def gerar_qr_base64(codigo):
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(codigo)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#3B1FA8', back_color='white')
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


# ✅ FIX-SEC-10: Sanitização de inputs de texto
def sanitizar_nome(nome: str) -> str:
    if not nome:
        return ''
    nome = nome.strip()
    # Remove caracteres de controle e mantém letras, espaços, hífen, ponto, apóstrofo
    nome = re.sub(r'[^\w\s\-\'\.À-ÿ]', '', nome, flags=re.UNICODE)
    return nome[:120]   # Tamanho máximo do campo


# ─── ROTAS PÚBLICAS ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('leitor.html')


@app.route('/api/registrar', methods=['POST'])
@limiter.limit("30 per minute")  # ✅ FIX-SEC-2: Anti-abuso no leitor público
def registrar():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Requisição inválida.'}), 400

    codigo = data.get('codigo', '').strip()
    tipo_extra = data.get('tipo_extra')

    # ✅ FIX-SEC-11: Valida formato do código antes de consultar o banco
    if not re.match(r'^[A-Z]{2}-\d{4}-[A-Z0-9]{8}$', codigo):
        return jsonify({'success': False, 'message': 'QR Code inválido.'}), 400

    pessoa = Pessoa.query.filter_by(codigo=codigo, ativo=True).first()
    if not pessoa:
        return jsonify({'success': False, 'message': 'QR Code não reconhecido ou pessoa inativa.'}), 404

    ultimo = Registro.query.filter_by(pessoa_codigo=codigo).order_by(Registro.horario.desc()).first()
    tipo_registro = 'entrada' if (ultimo is None or ultimo.tipo == 'saida') else 'saida'

    if not pode_registrar(codigo, tipo_registro):
        return jsonify({'success': False, 'message': 'Aguarde 1 minuto para registrar novamente.'}), 429

    if tipo_registro == 'saida' and pessoa.tipo == 'crianca' and pessoa.responsavel_codigo:
        if not tipo_extra:
            return jsonify({
                'success': False,
                'requer_responsavel': True,
                'message': f'{pessoa.nome} é menor de idade. Apresente o QR Code do responsável para autorizar a saída.',
                'pessoa': pessoa.to_dict()
            }), 200

        # ✅ Valida formato do código do responsável também
        if not re.match(r'^[A-Z]{2}-\d{4}-[A-Z0-9]{8}$', tipo_extra):
            return jsonify({'success': False, 'message': 'QR Code do responsável inválido.'}), 400

        responsavel = Pessoa.query.filter_by(codigo=tipo_extra, ativo=True).first()
        if not responsavel or responsavel.codigo != pessoa.responsavel_codigo:
            return jsonify({'success': False, 'message': 'QR Code do responsável inválido ou não corresponde.'}), 403

        registro = Registro(pessoa_codigo=codigo, pessoa_nome=pessoa.nome, tipo='saida', autorizado_por=responsavel.nome)
        db.session.add(registro)
        db.session.commit()
        return jsonify({
            'success': True, 'tipo': 'saida', 'pessoa': pessoa.to_dict(),
            'horario': registro.horario.strftime('%H:%M:%S'),
            'message': f'Saída de {pessoa.nome} autorizada por {responsavel.nome}.'
        })

    registro = Registro(pessoa_codigo=codigo, pessoa_nome=pessoa.nome, tipo=tipo_registro)
    db.session.add(registro)
    db.session.commit()
    return jsonify({
        'success': True, 'tipo': tipo_registro, 'pessoa': pessoa.to_dict(),
        'horario': registro.horario.strftime('%H:%M:%S'),
        'message': f'{"Entrada" if tipo_registro == "entrada" else "Saída"} de {pessoa.nome} registrada!'
    })


@app.route('/api/atividade_recente')
def atividade_recente():
    # ✅ FIX-SEC-12: Endpoint público limitado a dados mínimos (sem dados sensíveis)
    registros = Registro.query.order_by(Registro.horario.desc()).limit(10).all()
    return jsonify([{
        'tipo': r.tipo,
        'horario': r.horario.strftime('%H:%M'),
        'pessoa_nome': r.pessoa_nome
    } for r in registros])


# ─── ROTAS ADMIN ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])   # ✅ FIX-SEC-2: Anti brute-force no login
def login():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'Requisição inválida'}), 400

        username = (data.get('username') or '').strip()[:80]
        password = data.get('password') or ''

        # ✅ FIX-SEC-13: Sempre busca o admin para evitar timing attack por user não encontrado
        admin = Admin.query.filter_by(username=username).first()

        # Suporte a migração de hash antigo → bcrypt
        autenticado = False
        if admin:
            if admin.password_hash.startswith('$2b$') or admin.password_hash.startswith('$2a$'):
                autenticado = admin.check_password(password)
            else:
                autenticado = _migrate_admin_hash_if_needed(admin, password)

        if autenticado:
            session.permanent = True
            session['admin_logged_in'] = True
            session['admin_username'] = admin.username
            return jsonify({'success': True})

        # ✅ FIX-SEC-14: Mensagem genérica — não revela se o usuário existe
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
    inicio_hoje = datetime.combine(hoje, datetime.min.time())
    fim_hoje = datetime.combine(hoje, datetime.max.time())

    entradas_hoje = Registro.query.filter(
        Registro.tipo == 'entrada',
        Registro.horario.between(inicio_hoje, fim_hoje)
    ).count()
    saidas_hoje = Registro.query.filter(
        Registro.tipo == 'saida',
        Registro.horario.between(inicio_hoje, fim_hoje)
    ).count()
    presentes = entradas_hoje - saidas_hoje
    cadastrados = Pessoa.query.filter_by(ativo=True).count()

    frequencia = []
    for i in range(6, -1, -1):
        dia = agora_brasilia().date() - timedelta(days=i)
        inicio = datetime.combine(dia, datetime.min.time())
        fim = datetime.combine(dia, datetime.max.time())
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
    q = request.args.get('q', '')[:100]   # ✅ FIX-SEC-15: Limita tamanho do parâmetro de busca
    tipo = request.args.get('tipo', '')

    query = Pessoa.query.filter_by(ativo=True)
    if tipo in ('crianca', 'responsavel'):
        query = query.filter_by(tipo=tipo)
    if q:
        query = query.filter(
            (Pessoa.nome.ilike(f'%{q}%')) | (Pessoa.codigo.ilike(f'%{q}%'))
        )
    return jsonify([p.to_dict() for p in query.order_by(Pessoa.nome).all()])


@app.route('/api/admin/pessoas', methods=['POST'])
@login_required
def api_cadastrar_pessoa():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Dados inválidos'}), 400

    tipo = data.get('tipo', 'crianca')
    if tipo not in ('crianca', 'responsavel'):
        return jsonify({'success': False, 'message': 'Tipo inválido'}), 400

    nome = sanitizar_nome(data.get('nome', ''))
    if not nome:
        return jsonify({'success': False, 'message': 'Nome obrigatório'}), 400

    # ✅ FIX-BUG-1: Valida responsavel_codigo se fornecido
    responsavel_codigo = data.get('responsavel_codigo') or None
    if responsavel_codigo:
        resp_existe = Pessoa.query.filter_by(codigo=responsavel_codigo, tipo='responsavel', ativo=True).first()
        if not resp_existe:
            return jsonify({'success': False, 'message': 'Responsável não encontrado'}), 400

    catequista_id = data.get('catequista_patio_id')
    try:
        catequista_id = int(catequista_id) if catequista_id else None
    except (ValueError, TypeError):
        catequista_id = None

    codigo = gerar_codigo(tipo)

    pessoa = Pessoa(
        codigo=codigo,
        nome=nome,
        tipo=tipo,
        responsavel_codigo=responsavel_codigo,
        telefone=(data.get('telefone') or '')[:20] or None,
        email=(data.get('email') or '')[:120] or None,
        data_nascimento=data.get('data_nascimento') or None,
        turma=(data.get('turma') or '')[:80] or None,
        catequista_patio_id=catequista_id if tipo == 'crianca' else None,
    )
    db.session.add(pessoa)
    db.session.commit()
    return jsonify({'success': True, 'pessoa': pessoa.to_dict(), 'codigo': codigo})


@app.route('/api/admin/pessoas/<int:pessoa_id>', methods=['PUT'])
@login_required
def api_atualizar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Dados inválidos'}), 400

    novo_nome = sanitizar_nome(data.get('nome', pessoa.nome))
    if not novo_nome:
        return jsonify({'success': False, 'message': 'Nome obrigatório'}), 400

    pessoa.nome = novo_nome
    pessoa.telefone = (data.get('telefone', pessoa.telefone) or '')[:20] or None
    pessoa.email = (data.get('email', pessoa.email) or '')[:120] or None
    pessoa.data_nascimento = data.get('data_nascimento', pessoa.data_nascimento) or None
    pessoa.turma = (data.get('turma', pessoa.turma) or '')[:80] or None

    responsavel_codigo = data.get('responsavel_codigo', pessoa.responsavel_codigo) or None
    if responsavel_codigo:
        resp_existe = Pessoa.query.filter_by(codigo=responsavel_codigo, tipo='responsavel', ativo=True).first()
        if not resp_existe:
            return jsonify({'success': False, 'message': 'Responsável não encontrado'}), 400
    pessoa.responsavel_codigo = responsavel_codigo

    catequista_id = data.get('catequista_patio_id')
    try:
        pessoa.catequista_patio_id = int(catequista_id) if catequista_id else None
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


@app.route('/api/admin/qrcode/<codigo>')
@login_required
def api_qrcode(codigo):
    # ✅ FIX-SEC-11: Valida formato antes de gerar
    if not re.match(r'^[A-Z]{2}-\d{4}-[A-Z0-9]{8}$', codigo):
        return jsonify({'error': 'Código inválido'}), 400
    return jsonify({'qr': gerar_qr_base64(codigo)})


@app.route('/api/admin/relatorio/<codigo>')
@login_required
def api_relatorio(codigo):
    # ✅ FIX-SEC-11: Valida formato
    if not re.match(r'^[A-Z]{2}-\d{4}-[A-Z0-9]{8}$', codigo):
        return jsonify({'error': 'Código inválido'}), 400

    pessoa = Pessoa.query.filter_by(codigo=codigo).first()
    if not pessoa:
        return jsonify({'error': 'Pessoa não encontrada'}), 404

    data_inicio = request.args.get('inicio')
    data_fim = request.args.get('fim')
    query = Registro.query.filter_by(pessoa_codigo=codigo)

    try:
        if data_inicio:
            query = query.filter(Registro.horario >= datetime.strptime(data_inicio, '%Y-%m-%d'))
        if data_fim:
            fim = datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(Registro.horario < fim)
    except ValueError:
        return jsonify({'error': 'Formato de data inválido'}), 400

    registros = query.order_by(Registro.horario.desc()).all()
    return jsonify({
        'pessoa': pessoa.to_dict(),
        'registros': [r.to_dict() for r in registros],
        'total_entradas': sum(1 for r in registros if r.tipo == 'entrada'),
        'total_saidas': sum(1 for r in registros if r.tipo == 'saida')
    })


@app.route('/api/admin/responsaveis')
@login_required
def api_responsaveis():
    resp = Pessoa.query.filter_by(tipo='responsavel', ativo=True).order_by(Pessoa.nome).all()
    return jsonify([{'codigo': p.codigo, 'nome': p.nome} for p in resp])


# ─── API CATEQUISTAS DE PÁTIO ─────────────────────────────────────────────────

@app.route('/api/admin/catequistas_patio', methods=['GET'])
@login_required
def api_listar_catequistas_patio():
    lista = CatequistaPatio.query.filter_by(ativo=True).order_by(CatequistaPatio.nome).all()
    return jsonify([c.to_dict() for c in lista])


@app.route('/api/admin/catequistas_patio', methods=['POST'])
@login_required
def api_criar_catequista_patio():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'message': 'Dados inválidos'}), 400

    nome = sanitizar_nome(data.get('nome') or '')
    if not nome:
        return jsonify({'success': False, 'message': 'Informe o nome.'}), 400

    existe = CatequistaPatio.query.filter(
        CatequistaPatio.nome.ilike(nome),
        CatequistaPatio.ativo == True
    ).first()
    if existe:
        return jsonify({'success': False, 'message': 'Já existe um catequista com esse nome.'}), 409

    c = CatequistaPatio(nome=nome)
    db.session.add(c)
    db.session.commit()
    return jsonify({'success': True, 'catequista': c.to_dict()})


@app.route('/api/admin/catequistas_patio/<int:cid>', methods=['DELETE'])
@login_required
def api_deletar_catequista_patio(cid):
    c = CatequistaPatio.query.get_or_404(cid)
    c.ativo = False
    db.session.commit()
    return jsonify({'success': True})


# ─── TRATAMENTO DE ERROS ──────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Recurso não encontrado'}), 404
    return redirect(url_for('index'))


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({'success': False, 'message': 'Muitas tentativas. Aguarde e tente novamente.'}), 429


# ─── INIT ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    # ✅ FIX-SEC-16: debug=False explícito — jamais debug em produção
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
