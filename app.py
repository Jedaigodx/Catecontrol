from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import qrcode
import io
import base64
import os
import hashlib
from functools import wraps

# ─── TIMEZONE BRASIL ─────────────────────────────────────────

BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")

def agora_brasilia():
    return datetime.now(BRASILIA_TZ)

app = Flask(__name__)
app.secret_key = 'catecontrol-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///catecontrol.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── MODELS ───────────────────────────────────────────────────────────────────

class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)  # ex: 2024001-00
    nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'crianca' ou 'responsavel'
    responsavel_codigo = db.Column(db.String(20), nullable=True)  # código do responsável (se menor)
    telefone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    data_nascimento = db.Column(db.String(20), nullable=True)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime(timezone=True), default=agora_brasilia)

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
            'ativo': self.ativo
        }

class Registro(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pessoa_codigo = db.Column(db.String(20), nullable=False)
    pessoa_nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'entrada' ou 'saida'
    horario = db.Column(db.DateTime(timezone=True), default=agora_brasilia)
    autorizado_por = db.Column(db.String(120), nullable=True)  # para saída de menor

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

    def set_password(self, password):
        self.password_hash = hashlib.sha256(password.encode()).hexdigest()

    def check_password(self, password):
        return self.password_hash == hashlib.sha256(password.encode()).hexdigest()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def gerar_codigo(tipo='crianca'):
    """Gera código único: ANO + sequencial + sufixo (00=criança, 01=responsável)"""
    ano = datetime.now().year
    sufixo = '00' if tipo == 'crianca' else '01'
    # Contar registros existentes do tipo no ano atual
    count = Pessoa.query.filter(
        Pessoa.codigo.like(f'{ano}%'),
        Pessoa.tipo == tipo
    ).count()
    numero = str(count + 1).zfill(3)
    return f'{ano}{numero}-{sufixo}'

def pode_registrar(codigo, tipo_registro):
    """Verifica delay de 1 minuto para evitar registros duplicados"""
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

# ─── ROTAS PÚBLICAS ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('leitor.html')

@app.route('/api/registrar', methods=['POST'])
def registrar():
    data = request.get_json()
    codigo = data.get('codigo', '').strip()
    tipo_extra = data.get('tipo_extra')  # 'saida_autorizada' com código do responsável

    pessoa = Pessoa.query.filter_by(codigo=codigo, ativo=True).first()
    if not pessoa:
        return jsonify({'success': False, 'message': 'QR Code não reconhecido ou pessoa inativa.'}), 404

    # Determinar tipo de registro (entrada ou saída)
    ultimo = Registro.query.filter_by(pessoa_codigo=codigo).order_by(Registro.horario.desc()).first()
    
    if ultimo is None or ultimo.tipo == 'saida':
        tipo_registro = 'entrada'
    else:
        tipo_registro = 'saida'

    # Verifica delay de 1 minuto
    if not pode_registrar(codigo, tipo_registro):
        return jsonify({'success': False, 'message': 'Aguarde 1 minuto para registrar novamente.'}), 429

    # Saída de menor: precisa do QR do responsável
    if tipo_registro == 'saida' and pessoa.tipo == 'crianca' and pessoa.responsavel_codigo:
        if not tipo_extra:
            return jsonify({
                'success': False,
                'requer_responsavel': True,
                'message': f'{pessoa.nome} é menor de idade. Apresente o QR Code do responsável para autorizar a saída.',
                'pessoa': pessoa.to_dict()
            }), 200
        
        # Validar QR do responsável
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
        return jsonify({
            'success': True,
            'tipo': 'saida',
            'pessoa': pessoa.to_dict(),
            'horario': registro.horario.strftime('%H:%M:%S'),
            'message': f'Saída de {pessoa.nome} autorizada por {responsavel.nome}.'
        })

    # Registrar normalmente
    registro = Registro(
        pessoa_codigo=codigo,
        pessoa_nome=pessoa.nome,
        tipo=tipo_registro
    )
    db.session.add(registro)
    db.session.commit()

    return jsonify({
        'success': True,
        'tipo': tipo_registro,
        'pessoa': pessoa.to_dict(),
        'horario': registro.horario.strftime('%H:%M:%S'),
        'message': f'{"Entrada" if tipo_registro == "entrada" else "Saída"} de {pessoa.nome} registrada!'
    })

@app.route('/api/atividade_recente')
def atividade_recente():
    registros = Registro.query.order_by(Registro.horario.desc()).limit(10).all()
    return jsonify([r.to_dict() for r in registros])

# ─── ROTAS ADMIN ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        admin = Admin.query.filter_by(username=data.get('username')).first()
        if admin and admin.check_password(data.get('password')):
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

    # Frequência semanal (últimos 7 dias)
    frequencia = []
    for i in range(6, -1, -1):
        dia = agora_brasilia().date() - timedelta(days=i)
        inicio = datetime.combine(dia, datetime.min.time())
        fim = datetime.combine(dia, datetime.max.time())
        ent = Registro.query.filter(Registro.tipo == 'entrada', Registro.horario.between(inicio, fim)).count()
        sai = Registro.query.filter(Registro.tipo == 'saida', Registro.horario.between(inicio, fim)).count()
        frequencia.append({
            'dia': dia.strftime('%d/%m'),
            'entradas': ent,
            'saidas': sai
        })

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
    q = request.args.get('q', '')
    query = Pessoa.query.filter_by(ativo=True)
    if q:
        query = query.filter(
            (Pessoa.nome.ilike(f'%{q}%')) | (Pessoa.codigo.ilike(f'%{q}%'))
        )
    pessoas = query.order_by(Pessoa.nome).all()
    return jsonify([p.to_dict() for p in pessoas])

@app.route('/api/admin/pessoas', methods=['POST'])
@login_required
def api_cadastrar_pessoa():
    data = request.get_json()
    tipo = data.get('tipo', 'crianca')
    codigo = gerar_codigo(tipo)

    pessoa = Pessoa(
        codigo=codigo,
        nome=data['nome'],
        tipo=tipo,
        responsavel_codigo=data.get('responsavel_codigo'),
        telefone=data.get('telefone'),
        email=data.get('email'),
        data_nascimento=data.get('data_nascimento')
    )
    db.session.add(pessoa)
    db.session.commit()
    return jsonify({'success': True, 'pessoa': pessoa.to_dict(), 'codigo': codigo})

@app.route('/api/admin/pessoas/<int:pessoa_id>', methods=['PUT'])
@login_required
def api_atualizar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    data = request.get_json()
    pessoa.nome = data.get('nome', pessoa.nome)
    pessoa.telefone = data.get('telefone', pessoa.telefone)
    pessoa.email = data.get('email', pessoa.email)
    pessoa.data_nascimento = data.get('data_nascimento', pessoa.data_nascimento)
    pessoa.responsavel_codigo = data.get('responsavel_codigo', pessoa.responsavel_codigo)
    db.session.commit()
    return jsonify({'success': True, 'pessoa': pessoa.to_dict()})

@app.route('/api/admin/pessoas/<int:pessoa_id>', methods=['DELETE'])
@login_required
def api_deletar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    pessoa.ativo = False  # soft delete
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/admin/qrcode/<codigo>')
@login_required
def api_qrcode(codigo):
    qr_b64 = gerar_qr_base64(codigo)
    return jsonify({'qr': qr_b64})

@app.route('/api/admin/relatorio/<codigo>')
@login_required
def api_relatorio(codigo):
    pessoa = Pessoa.query.filter_by(codigo=codigo).first()
    if not pessoa:
        return jsonify({'error': 'Pessoa não encontrada'}), 404

    data_inicio = request.args.get('inicio')
    data_fim = request.args.get('fim')

    query = Registro.query.filter_by(pessoa_codigo=codigo)

    if data_inicio:
        query = query.filter(Registro.horario >= datetime.strptime(data_inicio, '%Y-%m-%d'))
    if data_fim:
        fim = datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1)
        query = query.filter(Registro.horario < fim)

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

# ─── INIT ─────────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        if not Admin.query.first():
            admin = Admin(username='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print('Admin criado: admin / admin123')

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
