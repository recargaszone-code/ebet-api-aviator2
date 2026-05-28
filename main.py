#!/usr/bin/env python3
# main.py - EBET Aviator - MELHORADO
import os
import time
import threading
import re
import random
import traceback
import logging
import requests
from datetime import datetime
from flask import Flask, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("aviator")

# ================= CONFIG =================
TELEGRAM_TOKEN  = "8742776802:AAHSzD1qTwCqMEOdoW9_pT2l5GfmMBWUZQY"
TELEGRAM_CHAT_ID = "7427648935"
PHONE    = "857789345"
PASSWORD = "max123ZICO"
URL      = "https://ebet.co.mz/games/go/spribe?id=aviator"

POLL_INTERVAL_MIN = 4   # segundos (era 20)
POLL_INTERVAL_MAX = 7   # segundos (era 40)
MAX_ACUMULADO     = 100  # era 50

app = Flask(__name__)

# Cada entrada: {"value": 2.84, "time": "09:41:30"}
historico_acumulado: list[dict] = []
historico_atual: list[float]    = []
_history_lock = threading.Lock()
_last_telegram = 0
_status = {"state": "iniciando", "last_value": None, "last_update": None, "total_captados": 0}

# ================= TELEGRAM =================
def send_telegram(msg: str, throttle: int = 15):
    global _last_telegram
    now = time.time()
    if now - _last_telegram < throttle:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        _last_telegram = now
    except Exception as e:
        log.warning(f"Telegram falhou: {e}")

# ================= DRIVER =================
def start_driver() -> webdriver.Chrome:
    log.info("Iniciando ChromeDriver...")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    if os.path.exists("/usr/bin/chromium"):
        opts.binary_location = "/usr/bin/chromium"

    svc = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service()
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(60)
    return driver

# ================= SCRAPING =================
def coletar_historico(driver) -> list[float]:
    """Extrai todos os valores 'payout' visíveis na tela."""
    vals = []
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, "div.payout")
        for el in elements:
            txt = el.text.strip()
            m = re.search(r"(\d+\.?\d*)", txt)
            if m:
                v = float(m.group(1))
                if 1.0 <= v <= 10000.0:   # sanity check
                    vals.append(v)
    except Exception:
        pass
    return vals

def registrar_novo_valor(novo_valor: float):
    """Adiciona ao acumulado com timestamp, evita duplicata consecutiva."""
    global historico_acumulado
    ts = datetime.now().strftime("%H:%M:%S")
    with _history_lock:
        if historico_acumulado and historico_acumulado[0]["value"] == novo_valor:
            return  # duplicata consecutiva, ignora
        historico_acumulado.insert(0, {"value": novo_valor, "time": ts})
        if len(historico_acumulado) > MAX_ACUMULADO:
            historico_acumulado.pop()

    _status["last_value"]   = novo_valor
    _status["last_update"]  = ts
    _status["total_captados"] += 1

    log.info(f"NOVO: {novo_valor:.2f}x  |  total={_status['total_captados']}")

    ultimos = ", ".join(f"{e['value']:.2f}x" for e in historico_acumulado[:10])
    send_telegram(
        f"✈️ *EBET AVIATOR*\n"
        f"Novo: *{novo_valor:.2f}x* às {ts}\n"
        f"Últimos 10: [{ultimos}]",
        throttle=15
    )

# ================= LOOP PRINCIPAL =================
def conectar_e_monitorar(driver):
    """Faz login, navega até o jogo e entra no loop de coleta."""
    global historico_atual
    wait = WebDriverWait(driver, 60)

    # 1. Abre URL
    log.info("Abrindo URL...")
    driver.get(URL)
    time.sleep(random.uniform(7, 10))

    # 2. Clique inicial no Aviator (antes do login)
    _clicar_aviator(driver, wait, "Pré-login")
    time.sleep(random.uniform(4, 7))

    # 3. Login
    _fazer_login(driver, wait)
    time.sleep(random.uniform(8, 12))

    # 4. Clique no Aviator (pós-login)
    _clicar_aviator(driver, wait, "Pós-login")
    time.sleep(random.uniform(10, 18))

    # 5. Entrar nos iframes
    _entrar_iframes(driver, wait)

    # 6. Aguarda histórico aparecer
    log.info("Aguardando div.payout...")
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.payout")))
    log.info("Histórico visível! Iniciando coleta...")
    _status["state"] = "monitorando"
    send_telegram("✅ *Aviator conectado!* Monitoramento iniciado.")

    historico_atual = coletar_historico(driver)
    # Preenche acumulado inicial sem notificar
    with _history_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        for v in historico_atual:
            historico_acumulado.append({"value": v, "time": ts})

    # 7. Loop de coleta
    erros_consecutivos = 0
    while True:
        try:
            novos = coletar_historico(driver)
            if not novos:
                erros_consecutivos += 1
                if erros_consecutivos >= 5:
                    raise RuntimeError("5 coletas vazias consecutivas — possível desconexão")
                time.sleep(3)
                continue

            erros_consecutivos = 0

            # Detecta novo valor no topo
            if novos[0] != historico_atual[0] if historico_atual else True:
                novo = novos[0]
                registrar_novo_valor(novo)
                historico_atual = novos[:]

        except RuntimeError:
            raise  # propaga para reconectar
        except Exception as e:
            log.warning(f"Erro na coleta: {e}")

        time.sleep(random.uniform(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX))


def _clicar_aviator(driver, wait, label: str):
    try:
        imgs = wait.until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "img.landing-page__item-image"))
        )
        for img in imgs:
            src = img.get_attribute("src") or ""
            if "aviator" in src.lower():
                driver.execute_script("arguments[0].click();", img)
                log.info(f"Clique Aviator [{label}] OK")
                return
        log.warning(f"Imagem Aviator não encontrada [{label}]")
    except Exception as e:
        log.warning(f"Falha clique [{label}]: {e}")


def _fazer_login(driver, wait):
    try:
        phone_input = wait.until(EC.presence_of_element_located((By.ID, "phone-input")))
        phone_input.clear()
        phone_input.send_keys(PHONE)
        pwd = driver.find_element(By.ID, "password-input")
        pwd.clear()
        pwd.send_keys(PASSWORD)
        btn = driver.find_element(By.CSS_SELECTOR, "input.btn-session")
        driver.execute_script("arguments[0].click();", btn)
        log.info("Login enviado.")
    except Exception as e:
        log.warning(f"Login pulado ou falhou: {e}")


def _entrar_iframes(driver, wait):
    # Iframe externo (spribe)
    try:
        iframe1 = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='spribe']")))
        driver.switch_to.frame(iframe1)
        log.info("Iframe externo OK.")
    except Exception as e:
        log.error(f"Falha iframe externo: {e}")
        raise

    # Iframe interno (spribegaming)
    try:
        iframe2 = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='spribegaming']")))
        driver.switch_to.frame(iframe2)
        log.info("Iframe interno OK. ✅ Dentro do Aviator!")
    except Exception as e:
        log.error(f"Falha iframe interno: {e}")
        raise


# ================= THREAD SCRAPER =================
def iniciar_scraper():
    _status["state"] = "iniciando"
    backoff = 30
    while True:
        driver = None
        try:
            _status["state"] = "conectando"
            driver = start_driver()
            conectar_e_monitorar(driver)
        except Exception as e:
            _status["state"] = "erro"
            log.error(f"ERRO PRINCIPAL: {type(e).__name__} — {e}")
            traceback.print_exc()
            send_telegram(f"🔥 *Erro:* `{type(e).__name__}` — reconectando em {backoff}s")
            time.sleep(backoff + random.uniform(0, 15))
            backoff = min(backoff + 10, 120)  # backoff progressivo até 2min
        else:
            backoff = 30  # reset ao reconectar com sucesso
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            time.sleep(10)

# ================= API =================
@app.route("/api/history")
def api_history():
    """
    Retorna o histórico acumulado.
    Cada item: {"value": 2.84, "time": "09:41:30"}
    """
    with _history_lock:
        data = list(historico_acumulado)
    return jsonify({
        "count":       len(data),
        "history":     data,
        "last_value":  _status["last_value"],
        "last_update": _status["last_update"],
        "state":       _status["state"],
        "total":       _status["total_captados"],
    })

@app.route("/api/status")
def api_status():
    return jsonify(_status)

@app.route("/health")
def health():
    ok = _status["state"] == "monitorando"
    return jsonify({"ok": ok, "state": _status["state"]}), 200 if ok else 503

@app.route("/")
def home():
    s = _status
    return (
        f"<h2>EBET Aviator Monitor</h2>"
        f"<p>Estado: <b>{s['state']}</b></p>"
        f"<p>Último valor: <b>{s['last_value']}x</b> às {s['last_update']}</p>"
        f"<p>Total captados: <b>{s['total_captados']}</b></p>"
        f"<p><a href='/api/history'>/api/history</a> | <a href='/health'>/health</a></p>"
    )

# ================= MAIN =================
if __name__ == "__main__":
    threading.Thread(target=iniciar_scraper, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    # Usa threaded=True para suportar múltiplos clientes simultâneos
    app.run(host="0.0.0.0", port=port, threaded=True)
