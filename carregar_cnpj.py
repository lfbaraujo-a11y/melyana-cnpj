"""
Carrega os dados abertos do CNPJ (Receita Federal) no Postgres do Supabase.

Roda no GitHub Actions (ou em qualquer máquina com Python 3.9+ e psycopg):
baixa um .zip por vez da Receita, filtra, grava no banco com COPY e apaga o .zip.
Nada da base original fica guardado.

Variáveis de ambiente
  DESTINO       supabase (padrão) | bigquery  (BigQuery usa BQ_PROJECT, BQ_DATASET=receita, BQ_LOCATION)
  DATABASE_URL  conexão do Supabase (Session pooler, porta 5432). Obrigatória,
                exceto com DRY_RUN=1.
  RECORTE       sudeste-b2b (padrão) | rj | brasil-ativos | completa
  UFS           sobrepõe as UFs do recorte, ex.: "RJ SP"  (TODAS = sem filtro)
  CNAES         "b2b" (arquivo cnaes_b2b.txt) | "todos"   (sobrepõe o recorte)
  SO_ATIVOS     1 (padrão do recorte) ou 0
  SOCIOS        1 para carregar os sócios (padrão: só no recorte completa e sudeste-b2b)
  MES           AAAA-MM (padrão: o mais recente)
  URL_MES       endereço da pasta do mês, se a Receita mudar o site
  DRY_RUN       1 = só baixa, filtra e conta; não grava no banco
  ZIPS_LOCAIS   pasta com .zip já baixados (para teste)
  RETOMAR       1 = continua uma carga interrompida (BigQuery): não apaga o dataset e pula
                os arquivos já gravados (lista em PROGRESSO e no log LOG_ANTERIOR)
"""
import base64
import csv
import io
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile

# Desde 2026 a Receita publica num compartilhamento público do SERPRO+ (Nextcloud).
# Lista e baixa pela interface WebDAV pública do compartilhamento.
RF_SHARE = os.environ.get("RF_SHARE", "gn672Ad4CF8N6TK")
RF_DAV = os.environ.get("RF_DAV", "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ/")
AQUI = os.path.dirname(os.path.abspath(__file__))
TMP = os.environ.get("RUNNER_TEMP") or "/tmp"
SCHEMA = "receita"
RETOMAR = os.environ.get("RETOMAR", "") == "1"
PROGRESSO = os.path.expanduser(os.environ.get("PROGRESSO", "~/carga-cnpj.feito"))
LOG_ANTERIOR = os.path.expanduser(os.environ.get("LOG_ANTERIOR", "~/carga-cnpj.log"))

RECORTES = {
    "sudeste-b2b":   dict(ufs={"RJ", "SP", "MG", "ES"}, cnaes="b2b",   ativos=True,  socios=True,  colunas="essenciais"),
    "rj":            dict(ufs={"RJ"},                   cnaes="todos", ativos=True,  socios=False, colunas="essenciais"),
    "brasil-ativos": dict(ufs=None,                     cnaes="todos", ativos=True,  socios=False, colunas="essenciais"),
    "completa":      dict(ufs=None,                     cnaes="todos", ativos=False, socios=True,  colunas="todas"),
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ config
def config():
    nome = os.environ.get("RECORTE", "sudeste-b2b").strip() or "sudeste-b2b"
    if nome not in RECORTES:
        raise SystemExit(f"RECORTE inválido: {nome}. Use: {', '.join(RECORTES)}")
    c = dict(RECORTES[nome], nome=nome)
    if os.environ.get("UFS", "").strip():
        u = os.environ["UFS"].upper().split()
        c["ufs"] = None if u == ["TODAS"] else set(u)
    if os.environ.get("CNAES", "").strip():
        c["cnaes"] = os.environ["CNAES"].strip().lower()
    if os.environ.get("SO_ATIVOS", "").strip():
        c["ativos"] = os.environ["SO_ATIVOS"].strip() == "1"
    if os.environ.get("SOCIOS", "").strip():
        c["socios"] = os.environ["SOCIOS"].strip() == "1"
    c["cnae_set"] = None
    if c["cnaes"] == "b2b":
        c["cnae_set"] = set()
        with open(os.path.join(AQUI, "cnaes_b2b.txt"), encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha and not linha.startswith("#"):
                    c["cnae_set"].add(linha.split(";")[0].strip())
    c["dry"] = os.environ.get("DRY_RUN", "") == "1"
    return c


# ------------------------------------------------------------------ download
def abrir(url, headers=None, timeout=120, metodo="GET"):
    req = urllib.request.Request(url, method=metodo, headers={"User-Agent": "Mozilla/5.0 carregar-cnpj"})
    if "/public.php/webdav" in url:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{RF_SHARE}:".encode()).decode())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def listar(url):
    """Nomes (arquivos e pastas) dentro de uma pasta do WebDAV."""
    with abrir(url, {"Depth": "1"}, timeout=120, metodo="PROPFIND") as r:
        xml = r.read().decode("utf-8", "ignore")
    nomes = [urllib.parse.unquote(h).rstrip("/").split("/")[-1] for h in re.findall(r"<d:href>([^<]+)</d:href>", xml)]
    return [n for n in nomes[1:] if n]


def pasta_do_mes():
    if os.environ.get("URL_MES"):
        return os.environ["URL_MES"].rstrip("/") + "/"
    mes = os.environ.get("MES", "").strip()
    for t in range(1, 6):
        try:
            meses = sorted(n for n in listar(RF_DAV) if re.fullmatch(r"\d{4}-\d{2}", n))
            break
        except Exception as e:  # noqa
            log(f"   não consegui listar {RF_DAV} ({e}); tentativa {t}/5")
            time.sleep(20 * t)
    else:
        raise SystemExit("Não achei a pasta dos dados abertos da Receita.")
    return RF_DAV + (mes if mes in meses else meses[-1]) + "/"


def arquivos_do_mes(url_mes):
    return sorted(n for n in listar(url_mes) if n.lower().endswith(".zip"))


def baixar(url, destino, tentativas=8):
    tmp = destino + ".parcial"
    for t in range(1, tentativas + 1):
        try:
            ja = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            with abrir(url, {"Range": f"bytes={ja}-"} if ja else None, timeout=300) as r:
                if ja and r.status != 206:
                    ja = 0
                with open(tmp, "ab" if ja else "wb") as f:
                    feito, ultimo = ja, time.time()
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        feito += len(b)
                        if time.time() - ultimo > 60:
                            log(f"   {os.path.basename(destino)}: {feito / 1e6:.0f} MB")
                            ultimo = time.time()
            os.replace(tmp, destino)
            return destino
        except Exception as e:  # noqa
            log(f"   falhou ({e}); tentativa {t}/{tentativas}")
            time.sleep(min(120, 10 * t))
    raise SystemExit(f"Não consegui baixar {url}")


class Fonte:
    """Entrega os .zip um por vez e apaga cada um depois de lido."""

    def __init__(self):
        self.local = os.environ.get("ZIPS_LOCAIS")
        if self.local:
            self.lista = sorted(f for f in os.listdir(self.local) if f.lower().endswith(".zip"))
            log(f"Usando .zip locais em {self.local}")
        else:
            self.url = pasta_do_mes()
            self.lista = arquivos_do_mes(self.url)
            log(f"Pasta do mês: {self.url} ({len(self.lista)} arquivos)")
        self.mes = re.search(r"(\d{4}-\d{2})", getattr(self, "url", "") or "")
        self.mes = self.mes.group(1) if self.mes else time.strftime("%Y-%m")

    def grupo(self, prefixo):
        return [f for f in self.lista if re.fullmatch(prefixo + r"\d*\.zip", f, re.I)]

    def linhas(self, nome):
        if self.local:
            caminho, apagar = os.path.join(self.local, nome), False
        else:
            log(f"Baixando {nome}")
            caminho, apagar = baixar(self.url + nome, os.path.join(TMP, nome)), True
        try:
            with zipfile.ZipFile(caminho) as z:
                for interno in z.namelist():
                    with z.open(interno) as f:
                        texto = io.TextIOWrapper(f, encoding="latin-1", newline="")
                        for row in csv.reader(texto, delimiter=";", quotechar='"'):
                            yield [v.replace("\x00", "").strip() for v in row]
        finally:
            if apagar and os.path.exists(caminho):
                os.remove(caminho)
                log(f"   {nome} apagado")


# ------------------------------------------------------------------ banco
DDL_ESSENCIAIS = f"""
create table {SCHEMA}.estabelecimento (
  cnpj text primary key, cnpj_basico text not null, matriz_filial smallint,
  nome_fantasia text, situacao smallint, data_situacao date, data_inicio date,
  cnae_principal text, cnae_secundaria text,
  logradouro text, numero text, complemento text, bairro text, cep text,
  uf text, municipio text,
  ddd1 text, telefone1 text, ddd2 text, telefone2 text, email text
);"""

DDL_TODAS = f"""
create table {SCHEMA}.estabelecimento (
  cnpj text primary key, cnpj_basico text not null, matriz_filial smallint,
  nome_fantasia text, situacao smallint, data_situacao date, motivo_situacao text,
  cidade_exterior text, pais text, data_inicio date,
  cnae_principal text, cnae_secundaria text,
  logradouro text, numero text, complemento text, bairro text, cep text,
  uf text, municipio text,
  ddd1 text, telefone1 text, ddd2 text, telefone2 text, ddd_fax text, fax text,
  email text, situacao_especial text, data_situacao_especial date
);"""

DDL_RESTO = f"""
create table {SCHEMA}.empresa (
  cnpj_basico text primary key, razao_social text, natureza_juridica text,
  qualificacao_responsavel text, capital_social numeric, porte text, ente_federativo text
);
create table {SCHEMA}.simples (
  cnpj_basico text primary key, opcao_simples text, data_opcao_simples date,
  data_exclusao_simples date, opcao_mei text, data_opcao_mei date, data_exclusao_mei date
);
create table {SCHEMA}.socio (
  cnpj_basico text, identificador smallint, nome text, qualificacao text,
  data_entrada date, faixa_etaria text
);
create table {SCHEMA}.cnae (codigo text primary key, descricao text);
create table {SCHEMA}.municipio (codigo text primary key, descricao text);
create table {SCHEMA}.natureza (codigo text primary key, descricao text);
create table {SCHEMA}.qualificacao (codigo text primary key, descricao text);
create table {SCHEMA}.motivo (codigo text primary key, descricao text);
create table {SCHEMA}.carga (mes text, recorte text, carregado_em timestamptz default now(),
  estabelecimentos bigint, empresas bigint, socios bigint);
"""

INDICES = f"""
create index on {SCHEMA}.estabelecimento (cnpj_basico);
create index on {SCHEMA}.estabelecimento (cnae_principal);
create index on {SCHEMA}.estabelecimento (uf, municipio);
create index on {SCHEMA}.socio (cnpj_basico);
"""

VIEW = f"""
create or replace view {SCHEMA}.empresas as
select e.cnpj,
       case e.matriz_filial when 1 then 'Matriz' when 2 then 'Filial' end as matriz_filial,
       emp.razao_social, e.nome_fantasia,
       case e.situacao when 1 then 'Nula' when 2 then 'Ativa' when 3 then 'Suspensa'
                       when 4 then 'Inapta' when 8 then 'Baixada' end as situacao,
       e.data_inicio, e.cnae_principal, c.descricao as atividade_principal, e.cnae_secundaria,
       e.uf, m.descricao as municipio, e.bairro,
       concat_ws(', ', nullif(e.logradouro,''), nullif(e.numero,''), nullif(e.complemento,'')) as endereco,
       e.cep,
       nullif(e.ddd1 || e.telefone1, '') as telefone1,
       nullif(e.ddd2 || e.telefone2, '') as telefone2,
       lower(nullif(e.email,'')) as email,
       case emp.porte when '01' then 'Micro empresa' when '03' then 'EPP' when '05' then 'Demais'
                      else 'Não informado' end as porte,
       emp.capital_social, n.descricao as natureza_juridica,
       (s.opcao_simples = 'S') as simples, (s.opcao_mei = 'S') as mei
from {SCHEMA}.estabelecimento e
left join {SCHEMA}.empresa emp on emp.cnpj_basico = e.cnpj_basico
left join {SCHEMA}.cnae c on c.codigo = e.cnae_principal
left join {SCHEMA}.municipio m on m.codigo = e.municipio
left join {SCHEMA}.natureza n on n.codigo = emp.natureza_juridica
left join {SCHEMA}.simples s on s.cnpj_basico = e.cnpj_basico;
"""


class Banco:
    def __init__(self, dry):
        self.dry = dry
        self.cont = {}
        if dry:
            log("DRY_RUN: nada será gravado")
            return
        import psycopg
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise SystemExit("Falta DATABASE_URL")
        self.con = psycopg.connect(url, autocommit=True)
        self.con.execute("set statement_timeout = 0")

    def sql(self, comando):
        if not self.dry:
            self.con.execute(comando)

    def inicio(self, c):
        self.sql(f"drop schema if exists {SCHEMA} cascade; create schema {SCHEMA};")
        self.sql(DDL_TODAS if c["colunas"] == "todas" else DDL_ESSENCIAIS)
        self.sql(DDL_RESTO)

    def fim(self, c, mes):
        log("Criando índices e a visão receita.empresas")
        self.sql(INDICES)
        self.sql(VIEW)
        self.sql(f"insert into {SCHEMA}.carga (mes, recorte, estabelecimentos, empresas, socios) values "
                 f"('{mes}', '{c['nome']}', {self.cont.get('estabelecimento', 0)}, "
                 f"{self.cont.get('empresa', 0)}, {self.cont.get('socio', 0)})")
        self.sql("analyze;")
        if not self.dry:
            tam = self.con.execute("select pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
            log(f"Tamanho do banco agora: {tam}")

    def copiar(self, tabela, colunas, linhas):
        n = 0
        if self.dry:
            for _ in linhas:
                n += 1
        else:
            with self.con.cursor() as cur:
                with cur.copy(f"copy {SCHEMA}.{tabela} ({', '.join(colunas)}) from stdin") as cp:
                    for row in linhas:
                        cp.write_row(row)
                        n += 1
        self.cont[tabela] = self.cont.get(tabela, 0) + n
        return n


# ------------------------------------------------------------------ BigQuery
def tipo_bq(col):
    if col.startswith("data_"):
        return "DATE"
    if col in ("matriz_filial", "situacao", "identificador", "estabelecimentos", "empresas", "socios"):
        return "INT64"
    if col == "capital_social":
        return "NUMERIC"
    return "STRING"


VIEW_BQ = VIEW.replace("create or replace view", "create or replace view").replace(
    "concat_ws(', ', nullif(e.logradouro,''), nullif(e.numero,''), nullif(e.complemento,''))",
    "array_to_string(array(select x from unnest([e.logradouro, e.numero, e.complemento]) x "
    "where x is not null), ', ')").replace(
    "nullif(e.ddd1 || e.telefone1, '')", "concat(e.ddd1, e.telefone1)").replace(
    "nullif(e.ddd2 || e.telefone2, '')", "concat(e.ddd2, e.telefone2)")


class BigQuery:
    """Grava cada lote num .csv.gz temporário, carrega com load job e apaga.
    Funciona no modo sandbox (sem cartão): não usa DML."""

    def __init__(self, dry):
        self.dry = dry
        self.cont = {}
        self.ds = os.environ.get("BQ_DATASET", SCHEMA)
        if dry:
            log("DRY_RUN: nada será gravado")
            return
        from google.cloud import bigquery
        self.bq = bigquery
        self.cli = bigquery.Client(project=os.environ.get("BQ_PROJECT") or None)
        self.ref = f"{self.cli.project}.{self.ds}"

    def sql(self, comando):
        if not self.dry:
            self.cli.query(comando).result()

    def inicio(self, c):
        if self.dry:
            return
        ds = self.bq.Dataset(self.ref)
        ds.location = os.environ.get("BQ_LOCATION", "southamerica-east1")
        if RETOMAR:
            log(f"Retomando: mantendo o dataset {self.ref}")
            return
        self.cli.delete_dataset(self.ref, delete_contents=True, not_found_ok=True)
        self.cli.create_dataset(ds)
        log(f"Dataset {self.ref} recriado")

    def linhas_na_tabela(self, tabela):
        try:
            return self.cli.get_table(f"{self.ref}.{tabela}").num_rows or 0
        except Exception:
            return 0

    def basicos_carregados(self):
        """CNPJs básicos já gravados em estabelecimento (para refazer o filtro na retomada)."""
        q = f"select distinct cnpj_basico from `{self.ref}.estabelecimento`"
        job = self.cli.query(q)
        try:
            tab = job.result().to_arrow(create_bqstorage_client=True)
            return tab.column(0).to_pylist()
        except Exception as e:
            log(f"   leitura rápida indisponível ({e}); lendo pela API comum")
            return (r[0] for r in job.result(page_size=100_000))

    def copiar(self, tabela, colunas, linhas):
        import gzip
        caminho = os.path.join(TMP, f"{tabela}.csv.gz")
        n = 0
        with gzip.open(caminho, "wt", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for row in linhas:
                w.writerow(["" if v is None else v for v in row])
                n += 1
        if not self.dry and n:
            cfg = self.bq.LoadJobConfig(
                source_format="CSV", write_disposition="WRITE_APPEND", allow_quoted_newlines=True,
                schema=[self.bq.SchemaField(col, tipo_bq(col)) for col in colunas],
                clustering_fields=["uf", "municipio", "cnae_principal"] if tabela == "estabelecimento" else None)
            with open(caminho, "rb") as f:
                self.cli.load_table_from_file(f, f"{self.ref}.{tabela}", job_config=cfg).result()
        os.remove(caminho)
        self.cont[tabela] = self.cont.get(tabela, 0) + n
        return n

    def fim(self, c, mes):
        log("Criando a visão receita.empresas")
        if not self.dry:
            self.sql(re.sub(SCHEMA + r"\.(\w+)", lambda m: f"`{self.ref}.{m.group(1)}`", VIEW_BQ))
        self.copiar("carga", ["mes", "recorte", "estabelecimentos", "empresas", "socios"],
                    [[mes, c["nome"], self.cont.get("estabelecimento", 0),
                      self.cont.get("empresa", 0), self.cont.get("socio", 0)]])


# ------------------------------------------------------------------ conversões
def data(v):
    v = v.strip()
    if len(v) == 8 and v.isdigit() and v != "00000000" and v[4:6] != "00":
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return None


def inteiro(v):
    return int(v) if v.strip().isdigit() else None


def nulo(v):
    return v if v != "" else None


def capital(v):
    try:
        return float(v.replace(".", "").replace(",", ".")) if v else None
    except ValueError:
        return None


# ------------------------------------------------------------------ retomada
def marcar_feito(nome):
    with open(PROGRESSO, "a") as f:
        f.write(nome + "\n")


def ja_feitos(db, fonte):
    """Arquivos que já estão no destino: lista PROGRESSO + log da carga anterior,
    conferidos com o número de linhas de cada tabela no BigQuery."""
    feito, soma = set(), {}
    if os.path.exists(PROGRESSO):
        feito |= set(open(PROGRESSO).read().split())
    if os.path.exists(LOG_ANTERIOR):
        atual = None
        for linha in open(LOG_ANTERIOR, encoding="utf-8", errors="replace"):
            m = re.search(r"Baixando (\S+\.zip)", linha)
            if m:
                atual = m.group(1)
                continue
            m = re.search(r" (cnae|municipio|natureza|qualificacao|motivo): \d+", linha)
            if m and atual:
                feito.add(atual)
                continue
            m = re.search(r" (estabelecimento|empresa|simples|socio): \+(\d+)", linha)
            if m and atual and atual not in soma.get("_arq", set()):
                feito.add(atual)
                soma[m.group(1)] = soma.get(m.group(1), 0) + int(m.group(2))
                soma.setdefault("_arq", set()).add(atual)
    for tabela, prefixo in [("estabelecimento", "Estabelecimentos"), ("empresa", "Empresas"),
                            ("simples", "Simples"), ("socio", "Socios")]:
        n_bq = db.linhas_na_tabela(tabela)
        esperado = soma.get(tabela, 0)
        pend = [f for f in fonte.grupo(prefixo) if f not in feito]
        if n_bq > esperado and pend:
            log(f"   {pend[0]} já estava gravado (BigQuery tem {n_bq}, log dizia {esperado})")
            feito.add(pend[0])
        elif n_bq < esperado:
            raise SystemExit(f"{tabela}: BigQuery tem {n_bq} linhas e o log diz {esperado}. Avise o Claude.")
        if n_bq:
            db.cont[tabela] = n_bq
    for f in sorted(feito):
        marcar_feito(f)
    log(f"Retomada: {len(feito)} arquivos já gravados; contagens atuais {db.cont}")
    return feito


# ------------------------------------------------------------------ principal
def main():
    c = config()
    log(f"Recorte: {c['nome']} | UFs: {' '.join(sorted(c['ufs'])) if c['ufs'] else 'todas'} | "
        f"CNAEs: {c['cnaes']} ({len(c['cnae_set']) if c['cnae_set'] else 'todos'}) | "
        f"só ativos: {c['ativos']} | sócios: {c['socios']} | colunas: {c['colunas']}")
    fonte = Fonte()
    destino = os.environ.get("DESTINO", "supabase").strip().lower()
    db = BigQuery(c["dry"]) if destino == "bigquery" else Banco(c["dry"])
    db.inicio(c)
    retomar = RETOMAR and destino == "bigquery" and not c["dry"]
    if retomar:
        feito = ja_feitos(db, fonte)
    else:
        feito = set()
        if os.path.exists(PROGRESSO):
            os.remove(PROGRESSO)

    # tabelas de domínio
    for prefixo, tabela in [("Cnaes", "cnae"), ("Municipios", "municipio"), ("Naturezas", "natureza"),
                            ("Qualificacoes", "qualificacao"), ("Motivos", "motivo")]:
        for nome in fonte.grupo(prefixo):
            if nome in feito:
                continue
            n = db.copiar(tabela, ["codigo", "descricao"],
                          ([r[0], r[1]] for r in fonte.linhas(nome) if len(r) >= 2))
            log(f"{tabela}: {n}")
            marcar_feito(nome)

    # estabelecimentos (decidem quais empresas entram)
    basicos = bytearray(12_500_000)  # 1 bit por CNPJ básico (8 dígitos): 12,5 MB em vez de GBs num set
    ufs, cnaes, ativos, todas = c["ufs"], c["cnae_set"], c["ativos"], c["colunas"] == "todas"
    filtra_basico = bool(ufs or cnaes or ativos)

    def estab(linhas):
        for r in linhas:
            if len(r) < 30:
                continue
            if ativos and r[5] != "02":
                continue
            if ufs and r[19] not in ufs:
                continue
            if cnaes and r[11] not in cnaes and not (cnaes & set(r[12].split(","))):
                continue
            if filtra_basico and r[0].isdigit():
                n = int(r[0])
                basicos[n >> 3] |= 1 << (n & 7)
            log_c = (" ".join(x for x in (r[13], r[14]) if x)).strip()
            if todas:
                yield [r[0] + r[1] + r[2], r[0], inteiro(r[3]), nulo(r[4]), inteiro(r[5]), data(r[6]),
                       nulo(r[7]), nulo(r[8]), nulo(r[9]), data(r[10]), r[11], nulo(r[12]),
                       nulo(log_c), nulo(r[15]), nulo(r[16]), nulo(r[17]), nulo(r[18]), r[19], r[20],
                       nulo(r[21]), nulo(r[22]), nulo(r[23]), nulo(r[24]), nulo(r[25]), nulo(r[26]),
                       nulo(r[27]), nulo(r[28]), data(r[29])]
            else:
                yield [r[0] + r[1] + r[2], r[0], inteiro(r[3]), nulo(r[4]), inteiro(r[5]), data(r[6]),
                       data(r[10]), r[11], nulo(r[12]), nulo(log_c), nulo(r[15]), nulo(r[16]),
                       nulo(r[17]), nulo(r[18]), r[19], r[20],
                       nulo(r[21]), nulo(r[22]), nulo(r[23]), nulo(r[24]), nulo(r[27])]

    cols_e = (["cnpj", "cnpj_basico", "matriz_filial", "nome_fantasia", "situacao", "data_situacao",
               "motivo_situacao", "cidade_exterior", "pais", "data_inicio", "cnae_principal",
               "cnae_secundaria", "logradouro", "numero", "complemento", "bairro", "cep", "uf",
               "municipio", "ddd1", "telefone1", "ddd2", "telefone2", "ddd_fax", "fax", "email",
               "situacao_especial", "data_situacao_especial"] if todas else
              ["cnpj", "cnpj_basico", "matriz_filial", "nome_fantasia", "situacao", "data_situacao",
               "data_inicio", "cnae_principal", "cnae_secundaria", "logradouro", "numero",
               "complemento", "bairro", "cep", "uf", "municipio", "ddd1", "telefone1", "ddd2",
               "telefone2", "email"])
    if retomar and filtra_basico and any(f in feito for f in fonte.grupo("Estabelecimentos")):
        log("Refazendo o filtro com os CNPJs já gravados (alguns minutos)...")
        k = 0
        for b in db.basicos_carregados():
            if b and b.isdigit():
                n = int(b)
                basicos[n >> 3] |= 1 << (n & 7)
                k += 1
        log(f"   {k} CNPJs básicos")
    for nome in fonte.grupo("Estabelecimentos"):
        if nome in feito:
            continue
        n = db.copiar("estabelecimento", cols_e, estab(fonte.linhas(nome)))
        log(f"estabelecimento: +{n} (total {db.cont['estabelecimento']})")
        marcar_feito(nome)

    def do_recorte(r):
        if not filtra_basico:
            return True
        if not r[0].isdigit():
            return False
        n = int(r[0])
        return bool(basicos[n >> 3] & (1 << (n & 7)))

    for nome in fonte.grupo("Empresas"):
        if nome in feito:
            continue
        n = db.copiar("empresa", ["cnpj_basico", "razao_social", "natureza_juridica",
                                  "qualificacao_responsavel", "capital_social", "porte", "ente_federativo"],
                      ([r[0], nulo(r[1]), nulo(r[2]), nulo(r[3]), capital(r[4]), nulo(r[5]), nulo(r[6])]
                       for r in fonte.linhas(nome) if len(r) >= 7 and do_recorte(r)))
        log(f"empresa: +{n}")
        marcar_feito(nome)

    for nome in fonte.grupo("Simples"):
        if nome in feito:
            continue
        n = db.copiar("simples", ["cnpj_basico", "opcao_simples", "data_opcao_simples",
                                  "data_exclusao_simples", "opcao_mei", "data_opcao_mei", "data_exclusao_mei"],
                      ([r[0], nulo(r[1]), data(r[2]), data(r[3]), nulo(r[4]), data(r[5]), data(r[6])]
                       for r in fonte.linhas(nome) if len(r) >= 7 and do_recorte(r)))
        log(f"simples: +{n}")
        marcar_feito(nome)

    if c["socios"]:
        # sem CPF/CNPJ do sócio nem representante: só nome, qualificação e entrada
        for nome in fonte.grupo("Socios"):
            if nome in feito:
                continue
            n = db.copiar("socio", ["cnpj_basico", "identificador", "nome", "qualificacao",
                                    "data_entrada", "faixa_etaria"],
                          ([r[0], inteiro(r[1]), nulo(r[2]), nulo(r[4]), data(r[5]), nulo(r[10])]
                           for r in fonte.linhas(nome) if len(r) >= 11 and do_recorte(r)))
            log(f"socio: +{n}")
            marcar_feito(nome)

    db.fim(c, fonte.mes)
    log(f"Pronto: {db.cont}")


if __name__ == "__main__":
    sys.exit(main())
