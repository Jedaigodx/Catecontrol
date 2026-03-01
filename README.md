# ⛪ CateControl — Sistema de Gestão de Catequese

Sistema SaaS para controle de entrada e saída de pessoas em catequese, com leitor QR Code, painel administrativo e relatórios.

## 📁 Estrutura do Projeto

```
catecontrol/
├── app.py                  # Backend Flask + API
├── Procfile                # conexão com servidor
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
