# app.py
import os
import io
import re
import json
import base64
import time
import datetime as dt
import requests
import pandas as pd
import numpy as np
import streamlit as st

# -----------------------------
# Config base
# -----------------------------
st.set_page_config(page_title="Pokemon Card Tracker", page_icon="🃏", layout="wide")

APP_TITLE = "Pokemon Card Tracker — Cardmarket"
DATA_DIR = "data"

# Mappatura file -> nome espansione come vuoi visualizzarlo nel filtro
EXPANSIONS = {
    "prezzi_pokemon_Surging-Sparks.xlsx": "Surging Sparks",
    "prezzi_pokemon_Paradox-Rift.xlsx": "Paradox Rift",
    "df_prezzi151-aggiornato-completo.xlsx": "151",
}

# Colonne attese
COL_CARD = "Carta"
COL_ID = "ID completo"
COL_LINK = "Link"
COL_5P = "Primi 5 Prezzi (IT, NM)"
COL_MED = "Media Prezzi (IT, NM)"

# -----------------------------
# Utility: parsing lista prezzi
# -----------------------------
_number_regex = re.compile(r"[-+]?\d*[\.,]?\d+")

def parse_price_list(value):
    """
    Converte il campo 'Primi 5 Prezzi (IT, NM)' in lista di float.
    Gestisce formati tipo:
      - "[1.20, 1.35, 1.50, 1.60, 1.75]"
      - "1.20; 1.35; 1.50; 1.60; 1.75"
      - "1,20 - 1,35 - 1,50 - 1,60 - 1,75"
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, (list, tuple)):
        # se è già lista, normalizza a float
        out = []
        for x in value:
            if x is None:
                continue
            try:
                out.append(float(str(x).replace(",", ".")))
            except:
                pass
        return out
    text = str(value)
    nums = _number_regex.findall(text)
    out = []
    for n in nums:
        try:
            out.append(float(n.replace(",", ".")))
        except:
            pass
    return out

def to_float(value):
    if value is None:
        return np.nan
    try:
        return float(str(value).replace(",", "."))
    except:
        return np.nan

# -----------------------------
# Cache: carica excel
# -----------------------------
@st.cache_data(show_spinner=True)
def load_one_excel(path, espansione):
    df = pd.read_excel(path, engine="openpyxl")
    # Tieni solo le colonne previste se presenti
    keep = [c for c in [COL_CARD, COL_ID, COL_LINK, COL_5P, COL_MED] if c in df.columns]
    df = df[keep].copy()

    # Aggiungi espansione
    df["Espansione"] = espansione

    # Normalizza prezzi
    if COL_MED in df.columns:
        df[COL_MED] = df[COL_MED].apply(to_float)
    if COL_5P in df.columns:
        df["Prezzi_Lista"] = df[COL_5P].apply(parse_price_list)
    else:
        df["Prezzi_Lista"] = [[] for _ in range(len(df))]

    # Chiave unica per preferiti
    df["CardKey"] = df["Espansione"].astype(str) + "|" + df[COL_ID].astype(str)

    return df

@st.cache_data(show_spinner=True)
def load_all_data(data_dir):
    frames = []
    missing = []
    for filename, esp in EXPANSIONS.items():
        path = os.path.join(data_dir, filename)
        if os.path.exists(path):
            frames.append(load_one_excel(path, esp))
        else:
            missing.append(filename)
    if not frames:
        return pd.DataFrame(), missing
    df = pd.concat(frames, ignore_index=True)
    # Rimuovi duplicati ovvi
    df = df.drop_duplicates(subset=["CardKey"])
    return df, missing

# -----------------------------
# Persistenza preferiti
#   - Opzione A: GitHub (consigliata per persistenza vera)
#   - Opzione B: file locale (fallback non garantito su Streamlit Cloud)
# -----------------------------
def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def read_favorites_from_github():
    token = st.secrets.get("GITHUB_TOKEN", None)
    repo = st.secrets.get("GH_REPO", None)  # es: "username/your-repo"
    branch = st.secrets.get("GH_BRANCH", "main")
    path = st.secrets.get("GH_FAV_PATH", "data/favorites.json")

    if not token or not repo:
        return None, None  # secrets non configurati
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    r = requests.get(url, headers=gh_headers(token), timeout=20)
    if r.status_code == 200:
        data = r.json()
        content_b64 = data.get("content", "")
        sha = data.get("sha", "")
        # decode
        decoded = base64.b64decode(content_b64).decode("utf-8")
        try:
            obj = json.loads(decoded)
        except:
            obj = {"users": {}}
        return obj, sha
    elif r.status_code == 404:
        # file non esiste ancora
        return {"users": {}}, None
    else:
        st.warning(f"GitHub GET fallita: {r.status_code} - {r.text}")
        return None, None

def write_favorites_to_github(new_obj, old_sha=None, msg="update favorites"):
    token = st.secrets.get("GITHUB_TOKEN", None)
    repo = st.secrets.get("GH_REPO", None)
    branch = st.secrets.get("GH_BRANCH", "main")
    path = st.secrets.get("GH_FAV_PATH", "data/favorites.json")

    if not token or not repo:
        return False

    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    content_str = json.dumps(new_obj, ensure_ascii=False, indent=2)
    payload = {
        "message": f"{msg} ({dt.datetime.utcnow().isoformat()}Z)",
        "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if old_sha:
        payload["sha"] = old_sha

    r = requests.put(url, headers=gh_headers(token), json=payload, timeout=20)
    if r.status_code in (200, 201):
        return True
    else:
        st.warning(f"GitHub PUT fallita: {r.status_code} - {r.text}")
        return False

LOCAL_FAV_FILE = os.path.join(DATA_DIR, ".favorites_local.json")

def read_favorites_local():
    if os.path.exists(LOCAL_FAV_FILE):
        try:
            with open(LOCAL_FAV_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"users": {}}
    return {"users": {}}

def write_favorites_local(new_obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(LOCAL_FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(new_obj, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.warning(f"Scrittura locale preferiti fallita: {e}")
        return False

def load_user_favorites(username):
    """
    Restituisce (set_di_cardkeys, backend) dove backend è "github" o "local"
    """
    # Prova GitHub
    obj, sha = read_favorites_from_github()
    if obj is not None:
        users = obj.get("users", {})
        arr = users.get(username, [])
        return set(arr), ("github", obj, sha)

    # Fallback: locale
    obj = read_favorites_local()
    users = obj.get("users", {})
    arr = users.get(username, [])
    return set(arr), ("local", obj, None)

def save_user_favorites(username, favorites_set, backend):
    """
    Salva preferiti a seconda del backend.
    backend = ("github"|"local", obj, sha)
    """
    backend_name, obj, sha = backend
    if obj is None:
        obj = {"users": {}}
    users = obj.get("users", {})
    users[username] = sorted(list(favorites_set))
    obj["users"] = users

    if backend_name == "github":
        ok = write_favorites_to_github(obj, old_sha=sha, msg=f"update favorites for {username}")
        return ok
    else:
        ok = write_favorites_local(obj)
        return ok

# -----------------------------
# UI Sidebar: utente e filtri
# -----------------------------
st.title(APP_TITLE)

with st.sidebar:
    st.header("👤 Utente")
    username = st.text_input(
        "Come ti chiami? (serve per ricaricare i tuoi Preferiti)",
        value=st.session_state.get("username", "")
    )
    if username:
        st.session_state["username"] = username.strip()

    st.markdown("---")
    st.caption("💾 Persistenza preferiti")
    if st.secrets.get("GITHUB_TOKEN", None):
        st.success("Modalità salvataggio: GitHub ✅")
    else:
        st.info("Modalità salvataggio: locale/temporanea ⚠️ (consigliato GitHub per persistenza)")

# Carica dati
with st.spinner("Caricamento dati..."):
    df, missing = load_all_data(DATA_DIR)

if missing:
    st.warning(
        "File mancanti nella cartella `data/`:\n- " + "\n- ".join(missing) +
        "\n\nCaricali nel repo per vederli nell'app."
    )

if df.empty:
    st.stop()

# Filtri
with st.sidebar:
    st.header("🔎 Filtri")
    espansioni_disponibili = sorted(df["Espansione"].unique())
    sel_esp = st.multiselect("Espansioni", espansioni_disponibili, default=espansioni_disponibili)

    query = st.text_input("Cerca per nome carta (parziale):", value="")
    sort_by = st.selectbox("Ordina per", [COL_MED, COL_CARD, "Espansione"], index=0)
    ascending = st.checkbox("Ordine crescente", value=True)

    show_only_favs = st.checkbox("Mostra solo Preferiti ⭐", value=False)

# Applica filtri
work = df.copy()
work = work[work["Espansione"].isin(sel_esp)]
if query.strip():
    q = query.strip().lower()
    work = work[work[COL_CARD].astype(str).str.lower().str.contains(q)]

# Ordina
if sort_by in work.columns:
    work = work.sort_values(by=sort_by, ascending=ascending, kind="mergesort")

# Carica preferiti utente
if not username:
    st.info("Inserisci un nome utente nella sidebar per abilitare i Preferiti.")
    user_favs = set()
    backend = ("local", {"users": {}}, None)
else:
    user_favs, backend = load_user_favorites(username)

# Aggiungi colonna boolean 'Preferito'
work = work.assign(Preferito=work["CardKey"].isin(user_favs))

# -----------------------------
# Vista: Tabella modificabile
# -----------------------------
st.subheader("📄 Carte")
st.caption("Suggerimento: modifica la colonna ⭐Preferito per aggiungere/rimuovere carte ai tuoi preferiti.")

# Prepara colonne per tabella visuale
view = work[[ "Espansione", COL_CARD, COL_ID, COL_LINK, COL_MED, "Prezzi_Lista", "Preferito", "CardKey" ]].copy()
# Colonna link cliccabile (rendering nella tabella via markdown)
def mk_link(url, text="Apri"):
    if pd.isna(url) or not str(url).startswith("http"):
        return ""
    return f"{url}"

view["Cardmarket"] = view[COL_LINK].apply(lambda u: mk_link(u, "Apri"))
view = view.drop(columns=[COL_LINK])

# Eventuale filtro "solo preferiti"
if show_only_favs:
    view = view[view["Preferito"] == True]

# Colonne da esporre
display_cols = ["Espansione", COL_CARD, COL_ID, "Cardmarket", COL_MED, "Prezzi_Lista", "Preferito", "CardKey"]

# Data Editor: editable sulla sola colonna Preferito
column_config = {
    "Cardmarket": st.column_config.LinkColumn("Cardmarket", help="Vai alla carta su Cardmarket"),
    COL_MED: st.column_config.NumberColumn("Prezzo medio (€)", format="%.2f"),
    "Prezzi_Lista": st.column_config.ListColumn("Ultimi 5 prezzi (€)"),
    "Preferito": st.column_config.CheckboxColumn("⭐ Preferito"),
    "CardKey": None,  # nascosta
}
edited = st.data_editor(
    view[display_cols],
    hide_index=True,
    column_config=column_config,
    disabled=["Espansione", COL_CARD, COL_ID, "Cardmarket", COL_MED, "Prezzi_Lista", "CardKey"],
    use_container_width=True,
    height=520,
    key="cards_editor",
)

# Sincronizza preferiti (diff tra edited e user_favs)
if username:
    edited_favs_keys = set(edited.loc[edited["Preferito"] == True, "CardKey"].tolist())
    if edited_favs_keys != user_favs:
        st.info("Hai modificato l'elenco di preferiti. Premi **Salva preferiti** per confermare.")

    c1, c2, c3 = st.columns([1,1,2])
    with c1:
        if st.button("💾 Salva preferiti", type="primary"):
            ok = save_user_favorites(username, edited_favs_keys, backend)
            if ok:
                st.success("Preferiti salvati!")
                # aggiorna cache locale di editor
                user_favs = edited_favs_keys
            else:
                st.error("Errore durante il salvataggio dei preferiti.")
    with c2:
        dl_obj = {"users": {username: sorted(list(edited_favs_keys))}}
        st.download_button(
            "⬇️ Esporta preferiti",
            data=json.dumps(dl_obj, ensure_ascii=False, indent=2),
            file_name=f"preferiti_{username}.json",
            mime="application/json",
        )
    with c3:
        up = st.file_uploader("⬆️ Importa preferiti (JSON)", type=["json"], label_visibility="visible")
        if up is not None:
            try:
                imported = json.load(up)
                arr = imported.get("users", {}).get(username, [])
                merged = set(arr).union(edited_favs_keys)
                ok = save_user_favorites(username, merged, backend)
                if ok:
                    st.success("Preferiti importati e salvati!")
                    # Forza refresh dei dati editor
                    st.experimental_rerun()
                else:
                    st.error("Errore salvataggio dopo l'import.")
            except Exception as e:
                st.error(f"File JSON non valido: {e}")

# -----------------------------
# Sezione: Dettaglio + mini-chart
# -----------------------------
st.markdown("---")
st.subheader("📊 Anteprime carte con mini-grafico prezzi (ultimi 5)")

# Tiny renderer: mostra una griglia con info principali e sparkline
# (Per performance limitiamo a max 200 righe visualizzate)
MAX_CARDS = 200
preview = work if not show_only_favs else work[work["CardKey"].isin(user_favs)]
preview = preview.head(MAX_CARDS)

cols = st.columns(3)
i = 0
for _, row in preview.iterrows():
    with cols[i % 3]:
        box = st.container(border=True)
        with box:
            st.markdown(f"**{row[COL_CARD]}**  \n*{row['Espansione']}*")
            # Link
            if isinstance(row[COL_LINK], str) and row[COL_LINK].startswith("http"):
                st.markdown(f"[Cardmarket]({row[COL_LINK]})", help="ApriCardmarket")
            # Prezzo medio
            med = row[COL_MED]
            if pd.notna(med):
                st.metric("Prezzo medio (€)", f"{med:.2f}")
            else:
                st.write("Prezzo medio: n/d")

            # Lista prezzi e mini chart
            prices = row["Prezzi_Lista"] if isinstance(row["Prezzi_Lista"], list) else []
            if prices:
                st.line_chart(prices, height=100)
                st.caption("Ultimi 5 prezzi: " + ", ".join([f"{p:.2f}€" for p in prices]))
            else:
                st.caption("Nessun dato prezzi recenti")

            # Toggle preferito locale (non salva automaticamente, rispetta flusso tabella)
            key = row["CardKey"]
            is_fav = key in user_favs
            new_val = st.toggle("⭐ Preferito", value=is_fav, key=f"fav_card_{key}", label_visibility="visible")
            if username and new_val != is_fav:
                # Aggiorna set in memoria e chiedi salvataggio
                if new_val:
                    user_favs.add(key)
                else:
                    if key in user_favs:
                        user_favs.remove(key)
                st.session_state["__pending_save__"] = True
    i += 1

if username and st.session_state.get("__pending_save__", False):
    st.info("Hai aggiunto/rimosso preferiti in anteprima. Premi **Salva preferiti** sopra per confermare.")
