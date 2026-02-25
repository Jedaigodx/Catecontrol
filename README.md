# ⛪ CateControl — Sistema de Gestão de Catequese

Sistema SaaS para controle de entrada e saída de pessoas em catequese, com leitor QR Code, painel administrativo e relatórios.

---

## 🚀 Como Rodar Localmente

### 1. Instalar dependências
```bash
pip install -r requirements.txt
```

### 2. Iniciar o servidor
```bash
python app.py
```

### 3. Acessar
- **Leitor QR (público):** http://localhost:5000
- **Painel Admin:** http://localhost:5000/admin
- **Login:** admin / admin123 ← **Troque a senha em produção!**

---

## 📁 Estrutura do Projeto

```
catecontrol/
├── app.py                  # Backend Flask + API
├── requirements.txt
├── templates/
│   ├── base.html           # Layout base (sidebar, estilos)
│   ├── leitor.html         # Página pública do leitor QR
│   ├── login.html          # Login do admin
│   ├── dashboard.html      # Dashboard com gráficos
│   ├── pessoas.html        # Listagem e gerenciamento
│   ├── cadastrar.html      # Cadastro + geração de QR
│   └── relatorios.html     # Relatórios por pessoa
└── instance/
    └── catecontrol.db      # Banco SQLite (gerado automaticamente)
```

---

## 🗂️ Lógica de Códigos

| Sufixo | Tipo       | Exemplo         |
|--------|------------|-----------------|
| -00    | Criança    | 2024001-00      |
| -01    | Responsável| 2024001-01      |

- A saída de crianças vinculadas a um responsável **exige o QR Code do responsável**.
- O sistema aguarda o segundo QR Code antes de registrar.
- Delay de **1 minuto** entre registros do mesmo código.

---

## ☁️ Hospedagem Recomendada

### Opção 1 — Railway.app (Recomendado para início)
- **Custo:** Free tier generoso / ~$5/mês
- **Como:**
  1. Crie conta em railway.app
  2. Conecte seu GitHub
  3. Adicione `Procfile` com: `web: gunicorn app:app`
  4. Defina variável `SECRET_KEY` nas env vars
  5. Deploy automático!

### Opção 2 — Render.com
- **Custo:** Free (com sleep) / $7/mês sem sleep
- Procfile: `web: gunicorn app:app`

### Opção 3 — VPS (mais controle)
- DigitalOcean Droplet $6/mês
- Use Nginx + Gunicorn + systemd
- Banco PostgreSQL em vez de SQLite para produção

---

## 🔒 Para Produção — Checklist

- [ ] Trocar `SECRET_KEY` por valor aleatório seguro
- [ ] Trocar senha do admin
- [ ] Usar PostgreSQL (não SQLite)
- [ ] Configurar HTTPS
- [ ] Adicionar variáveis de ambiente (não hardcode)

### Migrar para PostgreSQL:
```python
# app.py - substituir a linha do SQLite por:
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
```

```bash
pip install psycopg2-binary
```

### Procfile (para Railway/Render):
```
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

---

## 📱 Funcionalidades

- ✅ Leitor QR via câmera (html5-qrcode)
- ✅ Entrada manual de código
- ✅ Registro automático entrada/saída alternado
- ✅ Delay de 1 min anti-duplicata
- ✅ Autorização de saída de menores via QR do responsável
- ✅ Dashboard com gráficos (Chart.js)
- ✅ Cadastro com geração de QR Code imprimível
- ✅ Edição de dados cadastrais
- ✅ Desativação de pessoas (soft delete)
- ✅ Relatórios por pessoa com filtro de período
- ✅ Exportação CSV
