"""
Lumiere Chess — GitHub Actions Sync
Versão Python do script GAS, para múltiplos usuários.
Persistência local via data/*.json (sem leitura do Firestore).
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
import google.auth.transport.requests


# ─────────────────────────────────────────────
# CONFIGURAÇÕES
# ─────────────────────────────────────────────

FIREBASE_PROJECT_ID = "lumierechess"
FIRESTORE_DB        = "dblumiere"
BATCH_SIZE          = 1000          # igual ao GAS: lotes de 1000 partidas
DATA_DIR            = Path("data")

USERS_FILE     = DATA_DIR / "users.json"
LAST_SYNC_FILE = DATA_DIR / "last_sync.json"


# ─────────────────────────────────────────────
# AUTENTICAÇÃO FIREBASE (equivalente ao getFirestoreAccessToken do GAS)
# ─────────────────────────────────────────────

def get_firestore_token() -> str:
    """
    Gera um access token usando a conta de serviço do Firebase.
    Token válido por 1 hora. Para execuções longas (>1h), chame novamente.
    """
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise EnvironmentError("Secret FIREBASE_SERVICE_ACCOUNT não encontrado!")

    sa_info = json.loads(sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/datastore"]
    )
    auth_request = google.auth.transport.requests.Request()
    credentials.refresh(auth_request)
    return credentials.token


def renovar_token_se_necessario(token_info: dict) -> str:
    """Renova o token se tiver mais de 55 minutos desde a última geração."""
    gerado_em = token_info.get("gerado_em", 0)
    agora = time.time()
    if agora - gerado_em > 55 * 60:
        print("  [Token] Renovando access token do Firebase...")
        novo_token = get_firestore_token()
        token_info["token"]      = novo_token
        token_info["gerado_em"]  = agora
    return token_info["token"]


# ─────────────────────────────────────────────
# HELPERS FIRESTORE REST
# ─────────────────────────────────────────────

def firestore_patch(token: str, doc_path: str, doc_body: dict) -> dict:
    """
    Equivalente ao UrlFetchApp.fetch com method='PATCH' do GAS.
    doc_path: caminho relativo, ex: 'users/email%40x.com/historico/partidas_agregadas_1'
    """
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/{FIRESTORE_DB}/documents/{doc_path}"
    )
    resp = requests.patch(
        url,
        json=doc_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json"
        },
        timeout=60
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Firestore PATCH falhou ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


# ─────────────────────────────────────────────
# HELPERS DE ARQUIVO LOCAL
# ─────────────────────────────────────────────

def load_json(path: Path) -> dict | list:
    """Carrega um arquivo JSON. Retorna {} se não existir ou inválido."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path: Path, data: dict | list) -> None:
    """Salva dados em JSON com indentação."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def partidas_file(username: str) -> Path:
    """Retorna o caminho do arquivo de partidas local de um usuário."""
    return DATA_DIR / f"partidas_{username.lower()}.json"


# ─────────────────────────────────────────────
# LÓGICA DE RITMO (tradução exata do GAS)
# ─────────────────────────────────────────────

def calcular_ritmo(time_control: str) -> str:
    """
    Replica exatamente a função calcularRitmo() do GAS original.
    """
    tc = str(time_control)
    if re.match(r'^(600|900|1800)', tc):        return "rapid"
    if re.match(r'^(180|300)', tc):             return "blitz"
    if re.match(r'^(60|120)($|\+)', tc):        return "bullet"
    if re.search(r'1/86400|1/604800', tc):      return "daily"
    return "outros"


# ─────────────────────────────────────────────
# BUSCA CHESS.COM API
# ─────────────────────────────────────────────

def buscar_partidas_chess(username: str, last_end_time: int | None) -> list[dict]:
    """
    Equivalente ao loop while + UrlFetchApp.fetch do GAS.
    Retorna somente as partidas NOVAS (não processadas ainda).
    """
    hoje   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    limite = hoje - timedelta(days=365)

    novas_partidas = []
    links_vistos   = set()

    # Começa pelo mês atual e vai voltando
    ano  = hoje.year
    mes  = hoje.month

    while True:
        # Verifica se o fim do mês atual ainda está dentro do período de interesse
        if mes == 12:
            fim_mes = datetime(ano + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        else:
            fim_mes = datetime(ano, mes + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)

        if fim_mes < limite:
            break  # Mês inteiro fora do período — para

        mes_str = str(mes).zfill(2)
        url     = f"https://api.chess.com/pub/player/{username}/games/{ano}/{mes_str}"
        print(f"    Buscando {mes_str}/{ano}...")
        time.sleep(0.5)  # Respeita rate limit da API do Chess.com

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "lumiere-chess-sync/1.0 (github-actions)"},
                timeout=30
            )
            if resp.status_code == 200:
                partidas_mes = resp.json().get("games", [])

                for jogo in partidas_mes:
                    if not jogo.get("end_time"):
                        continue

                    data_jogo = datetime.fromtimestamp(jogo["end_time"], tz=timezone.utc)

                    if data_jogo < limite:
                        continue  # Fora do período de 365 dias

                    if jogo.get("rules") != "chess":
                        continue  # Ignora variantes (960, etc.)

                    link = jogo.get("url", "")
                    if link in links_vistos:
                        continue  # Duplicata dentro da resposta da API

                    # Se já processamos até este end_time, pula
                    # (evita reprocessar partidas antigas)
                    if last_end_time and jogo["end_time"] <= last_end_time:
                        continue

                    voce_branco = jogo["white"]["username"].lower() == username.lower()
                    lado        = jogo["white"] if voce_branco else jogo["black"]
                    adversario  = jogo["black"] if voce_branco else jogo["white"]
                    resultado   = lado["result"]

                    # ECO da abertura
                    eco = ""
                    pgn = jogo.get("pgn", "")
                    if pgn:
                        eco_match = re.search(r'\[ECO "([^"]+)"\]', pgn)
                        if eco_match:
                            eco = eco_match.group(1)

                    # Pontos (idêntico ao GAS)
                    if resultado == "win":
                        pontos = 1.0
                    elif resultado in {
                        "agreed", "stalemate", "repetition",
                        "insufficient", "50move", "timevsinsufficient"
                    }:
                        pontos = 0.5
                    else:
                        pontos = 0.0

                    novas_partidas.append({
                        "data":        data_jogo.isoformat(),
                        "adversario":  adversario["username"],
                        "cor":         "Brancas" if voce_branco else "Pretas",
                        "resultado":   resultado,
                        "rated":       bool(jogo.get("rated", False)),
                        "timeControl": str(jogo.get("time_control", "")),
                        "rating":      int(lado.get("rating", 0)),
                        "link":        link,
                        "eco":         eco,
                        "pontos":      pontos,
                        "end_time":    jogo["end_time"]  # usado para last_sync
                    })
                    links_vistos.add(link)

            elif resp.status_code == 404:
                print(f"    {mes_str}/{ano} — sem dados (404), continuando...")
            else:
                print(f"    {mes_str}/{ano} — status {resp.status_code}, pulando...")

        except Exception as e:
            print(f"    ERRO ao buscar {mes_str}/{ano}: {e}")

        # Volta um mês
        mes -= 1
        if mes == 0:
            mes = 12
            ano -= 1

    return novas_partidas


# ─────────────────────────────────────────────
# ENVIO DE PARTIDAS AO FIRESTORE (em lotes)
# ─────────────────────────────────────────────

def enviar_partidas_firestore(token: str, user_email: str, todas_partidas: list[dict]) -> None:
    """
    Equivalente ao loop de lotes do GAS.
    Envia partidas em lotes de BATCH_SIZE para:
    users/{email}/historico/partidas_agregadas_1, _2, ...
    """
    encoded_email = requests.utils.quote(user_email, safe="")
    total_lotes   = (len(todas_partidas) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"  Enviando {len(todas_partidas)} partidas em {total_lotes} lote(s)...")

    for i in range(0, len(todas_partidas), BATCH_SIZE):
        lote        = todas_partidas[i : i + BATCH_SIZE]
        index_lote  = (i // BATCH_SIZE) + 1

        # Monta o array de partidas no formato Firestore (idêntico ao GAS)
        partidas_values = []
        for p in lote:
            # Garante que a data está no formato ISO 8601 com Z
            data_iso = p["data"]
            if not data_iso.endswith("Z") and "+" not in data_iso[-6:]:
                data_iso = data_iso.split("+")[0] + "Z"

            partidas_values.append({
                "mapValue": {
                    "fields": {
                        "data":        {"timestampValue": data_iso},
                        "adversario":  {"stringValue":    p["adversario"]},
                        "cor":         {"stringValue":    p["cor"]},
                        "resultado":   {"stringValue":    p["resultado"]},
                        "rated":       {"booleanValue":   bool(p["rated"])},
                        "timeControl": {"stringValue":    str(p["timeControl"])},
                        "rating":      {"integerValue":   int(p["rating"])},
                        "link":        {"stringValue":    p["link"]},
                        "eco":         {"stringValue":    p["eco"]},
                        "pontos":      {"doubleValue":    float(p["pontos"])},
                        "ritmo":       {"stringValue":    calcular_ritmo(p["timeControl"])}
                    }
                }
            })

        doc_body = {
            "fields": {
                "partidas":  {"arrayValue": {"values": partidas_values}},
                "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            }
        }

        doc_name = f"partidas_agregadas_{index_lote}"
        doc_path = f"users/{encoded_email}/historico/{doc_name}"

        firestore_patch(token, doc_path, doc_body)
        print(f"  ✓ Lote {index_lote}/{total_lotes} ({len(lote)} partidas) salvo!")


# ─────────────────────────────────────────────
# RATING DIÁRIO (forward-fill — idêntico ao GAS)
# ─────────────────────────────────────────────

def gerar_e_enviar_rating_diario(token: str, user_email: str, todas_partidas: list[dict]) -> None:
    hoje   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    limite = hoje - timedelta(days=365)

    ritmos = ["rapid", "blitz", "bullet", "daily"]
    dados_por_ritmo: dict[str, list[dict]] = {r: [] for r in ritmos}

    for p in todas_partidas:
        if not p.get("rated"): continue
        try:
            data_p = datetime.fromisoformat(p["data"].replace("Z", "+00:00"))
        except: continue
        if data_p < limite or data_p > hoje: continue
        
        ritmo = calcular_ritmo(p["timeControl"])
        if ritmo in dados_por_ritmo:
            dados_por_ritmo[ritmo].append({"data": data_p, "rating": int(p["rating"])})

    dias_map: dict[str, dict] = {}
    data_iter = limite
    while data_iter <= hoje:
        data_str = data_iter.strftime("%Y-%m-%d")
        dias_map[data_str] = {r: None for r in ritmos}
        data_iter += timedelta(days=1)

    for ritmo in ritmos:
        partidas_ritmo = sorted(dados_por_ritmo[ritmo], key=lambda x: x["data"])
        
        # 1. Preenche os dias que tiveram partidas
        for p in partidas_ritmo:
            data_str = p["data"].strftime("%Y-%m-%d")
            if data_str in dias_map:
                dias_map[data_str][ritmo] = p["rating"]

        # 2. Forward-fill (IGUAL AO GAS: preenche lacunas com o último conhecido)
        last_rating = None
        for data_str in sorted(dias_map.keys()):
            if dias_map[data_str][ritmo] is not None:
                last_rating = dias_map[data_str][ritmo]
            elif last_rating is not None:
                dias_map[data_str][ritmo] = last_rating

        # 3. BACKWARD-FILL (O QUE ESTAVA FALTANDO): 
        # Se os dias iniciais ainda são None (não houve partida no começo do ano),
        # usamos o primeiro rating que aparecer no período (o mais antigo).
        primeiro_rating_do_periodo = next((p["rating"] for p in partidas_ritmo), 0)
        
        for data_str in sorted(dias_map.keys()):
            if dias_map[data_str][ritmo] is None:
                dias_map[data_str][ritmo] = primeiro_rating_do_periodo

    # Monta array final (removendo o risco de 0 no gráfico)
    dias_array = []
    for data_str, ratings in sorted(dias_map.items()):
        data_iso = datetime.strptime(data_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        dias_array.append({
            "data": data_iso,
            "ratings": {k: (v if v is not None else 0) for k, v in ratings.items()}
        })

    # ... (restante do código de envio ao Firestore continua igual)
    doc_body = {
        "fields": {
            "dias": {
                "arrayValue": {
                    "values": [
                        {
                            "mapValue": {
                                "fields": {
                                    "data": {"timestampValue": d["data"]},
                                    "ratings": {
                                        "mapValue": {
                                            "fields": {
                                                "rapid":  {"integerValue": d["ratings"]["rapid"]},
                                                "blitz":  {"integerValue": d["ratings"]["blitz"]},
                                                "bullet": {"integerValue": d["ratings"]["bullet"]},
                                                "daily":  {"integerValue": d["ratings"]["daily"]}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        for d in dias_array
                    ]
                }
            },
            "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        }
    }
    encoded_email = requests.utils.quote(user_email, safe="")
    doc_path      = f"users/{encoded_email}/stats/ratingDiario_agregado"
    firestore_patch(token, doc_path, doc_body)
    print(f"  ✓ ratingDiario_agregado atualizado com {len(dias_array)} dias.")


# ─────────────────────────────────────────────
# PROCESSAMENTO POR USUÁRIO
# ─────────────────────────────────────────────

def processar_usuario(
    usuario_info: dict,
    last_sync:    dict,
    token_info:   dict
) -> None:
    """Processa um único usuário: busca, merge, envia ao Firestore, salva estado."""
    username   = usuario_info["username"]
    user_email = usuario_info["email"]

    print(f"\n{'='*55}")
    print(f"👤 Processando: {username} ({user_email})")
    print(f"{'='*55}")

    # 1. Carrega histórico local de partidas
    pfile = partidas_file(username)
    local = load_json(pfile)
    partidas_existentes: list[dict] = local.get("partidas", []) if isinstance(local, dict) else []
    print(f"  Partidas locais carregadas: {len(partidas_existentes)}")

    # 2. Determina a partir de quando buscar
    last_end_time = last_sync.get(username, {}).get("last_end_time", None)
    if last_end_time:
        data_ultimo = datetime.fromtimestamp(last_end_time, tz=timezone.utc).strftime("%d/%m/%Y %H:%M")
        print(f"  Último sync: {data_ultimo}")
    else:
        print(f"  Primeiro sync — buscando todos os 12 meses.")

    # 3. Renova token se necessário (para execuções longas)
    token = renovar_token_se_necessario(token_info)

    # 4. Busca novas partidas na API do Chess.com
    novas_partidas = buscar_partidas_chess(username, last_end_time)
    print(f"  Novas partidas encontradas: {len(novas_partidas)}")

    if not novas_partidas:
        print(f"  ✓ Nada a atualizar. Pulando envio ao Firestore.")
        return

    # 5. Merge e deduplicação (equivalente ao unicasMap do GAS)
    links_existentes = {p["link"] for p in partidas_existentes}
    partidas_novas_unicas = [p for p in novas_partidas if p["link"] not in links_existentes]
    todas_partidas = partidas_existentes + partidas_novas_unicas

    # 6. Filtra para os últimos 365 dias
    hoje   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    limite = hoje - timedelta(days=365)
    todas_partidas = [
        p for p in todas_partidas
        if datetime.fromisoformat(p["data"].replace("Z", "+00:00")) >= limite
    ]

    # 7. Ordena por data decrescente (mais recente primeiro — igual ao GAS)
    todas_partidas.sort(key=lambda x: x["data"], reverse=True)
    print(f"  Total após merge (365d): {len(todas_partidas)}")

    # 8. Envia partidas ao Firestore em lotes
    token = renovar_token_se_necessario(token_info)
    enviar_partidas_firestore(token, user_email, todas_partidas)

    # 9. Gera e envia ratingDiario
    token = renovar_token_se_necessario(token_info)
    gerar_e_enviar_rating_diario(token, user_email, todas_partidas)

    # 10. Atualiza last_sync (maior end_time das partidas novas)
    max_end_time = max(p.get("end_time", 0) for p in novas_partidas)
    last_sync[username] = {
        "last_end_time": max_end_time,
        "updated_at":    datetime.now(timezone.utc).isoformat()
    }

    # 11. Salva histórico local atualizado
    save_json(pfile, {"partidas": todas_partidas})
    print(f"  ✓ Estado local salvo em {pfile}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    inicio = datetime.now(timezone.utc)
    print(f"\n{'#'*55}")
    print(f"  🏆 Lumiere Chess — GitHub Actions Sync")
    print(f"  Iniciado em: {inicio.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'#'*55}\n")

    # Carrega lista de usuários
    users_data = load_json(USERS_FILE)
    usuarios   = users_data.get("usuarios", [])
    if not usuarios:
        print("ERRO: Nenhum usuário em data/users.json!")
        return

    print(f"Usuários a processar: {len(usuarios)}")

    # Carrega estado de sincronização
    last_sync = load_json(LAST_SYNC_FILE)
    if isinstance(last_sync, list):
        last_sync = {}

    # Gera token inicial do Firebase (válido 1h)
    print("\nObtendo token do Firebase...")
    token_info = {
        "token":     get_firestore_token(),
        "gerado_em": time.time()
    }
    print("✓ Token obtido.\n")

    # Processa cada usuário
    erros = []
    for usuario_info in usuarios:
        try:
            processar_usuario(usuario_info, last_sync, token_info)
        except Exception as e:
            nome = usuario_info.get("username", "?")
            print(f"\n  ❌ ERRO ao processar {nome}: {e}")
            erros.append({"usuario": nome, "erro": str(e)})
            # Continua para o próximo usuário mesmo com erro

    # Salva last_sync atualizado
    save_json(LAST_SYNC_FILE, last_sync)

    fim      = datetime.now(timezone.utc)
    duracao  = (fim - inicio).seconds
    minutos  = duracao // 60
    segundos = duracao % 60

    print(f"\n{'#'*55}")
    print(f"  ✅ Sync concluído em {minutos}m {segundos}s")
    if erros:
        print(f"  ⚠️  {len(erros)} usuário(s) com erro: {[e['usuario'] for e in erros]}")
    print(f"{'#'*55}\n")


if __name__ == "__main__":
    main()