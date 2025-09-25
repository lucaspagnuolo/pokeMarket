# pokemarket.py
import os
import re
import json
import base64
import datetime as dt
import requests
import pandas as pd
import numpy as np
import streamlit as st

# ============== CONFIG DI BASE ==============
st.set_page_config(page_title="PokéMarket Tracker", page_icon="🃏", layout="wide")

APP_TITLE = "PokéMarket Tracker — Cardmarket"
DATA_DIR = "data"

# Nomi colonne attese
COL_CARD = "Carta"
COL_ID = "ID completo"
COL_LINK = "Link"
COL_5P = "Primi 5 Prezzi (IT, NM)"
COL_MED = "Media Prezzi (IT, NM)"

# Override (opzionale) per nomi espansioni in italiano
EXPANSION_NAME_OVERRIDES = {
    "Surging Sparks": "Scintille Folgoranti",
    "Paradox Rift": "Paradosso Temporale",
    "Destined Rivals": "Destini Rivali",
    "151":"151",
    "Prismatic Evolutions": "Evoluzioni Prismatiche",
    # Aggiungi qui eventuali altri override
}

# ============== UTILS ==============
_num_re = re.compile(r"[-+]?\d*[\.,]?\d+")

def parse_price_list(value):
    """Converte 'Primi 5 Prezzi (IT, NM)' in lista di float, tollerante a formati vari."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for x in value:
            if x is None:
                continue
            try:
                out.append(float(str(x).replace(",", ".")))
            except Exception:
                pass
        return out
    text = str(value)
    nums = _num_re.findall(text)
    out = []
    for n in nums:
        try:
            out.append(float(n.replace(",", ".")))
        except Exception:
            pass
    return out

def to_float(value):
    if value is None:
        return np.nan
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return np.nan

def prettify_expansion_label(filename: str) -> str:
    """
    Genera un nome umano leggibile a partire dal nome file.
    Esempi:
      - prezzi_pokemon_Prismatic-Evolutions.xlsx -> "Evoluzioni Prismatiche" (via override)
      - prezzi_pokemon_Destined-Rivals.xlsx     -> "Destini Rivali"
      - df_prezzi151-aggiornato-completo.xlsx   -> "151"
    """
    base = os.path.splitext(os.path.basename(filename))[0]

    # Caso speciale: set "151"
    if "151" in base:
        return "151"

    # Rimuovi prefissi comuni
    stem = re.sub(r'^(prezzi[_-]?pokemon[_-]?|df[_-]?prezzi[_-]?)', '', base, flags=re.IGNORECASE)

    # Pulisci separatori
    stem = stem.replace("_", " ").replace("-", " ")
    label = stem.strip()
    if not label:
        label = base

    # Title case (rispetta maiuscole/minuscole standard)
    label_tc = label.title()

    # Applica override italiano se presente
    return EXPANSION_NAME_OVERRIDES.get(label_tc, label_tc)

def discover_expansions(data_dir: str):
    """
    Scansiona la cartella data_dir e trova tutti i .xlsx.
    Ritorna:
      - mapping { filename -> expansion_label }
      - file_list ordinata
      - index_signature (per invalidare la cache quando i file cambiano)
    """
    files = []
    try:
        for fname in os.listdir(data_dir):
            if fname.lower().endswith(".xlsx"):
                files.append(fname)
    except FileNotFoundError:
        files = []

    files = sorted(files)  # ordine alfabetico

    mapping = {fname: prettify_expansion_label(fname) for fname in files}

    # Firma semplice della "versione" dei dati: (filename, size)
    index_signature = []
    for f in files:
        p = os.path.join(data_dir, f)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = -1
        index_signature.append((f, sz))
    index_signature = tuple(index_signature)

    return mapping, files, index_signature

# ============== CARICAMENTO DATI ==============
@st.cache_data(show_spinner=True)
def load_one_excel(path, espansione):
    df = pd.read_excel(path, engine="openpyxl")

    # Normalizza colonne essenziali se mancanti
    if COL_CARD not in df.columns:
        df[COL_CARD] = df.get("Nome", pd.Series([f"Carta {i}" for i in range(len(df))]))
    if COL_ID not in df.columns:
        df[COL_ID] = [f"{espansione}-{i}" for i in range(len(df))]
    if COL_LINK not in df.columns:
        df[COL_LINK] = ""
    if COL_5P not in df.columns:
        df[COL_5P] = ""
    if COL_MED not in df.columns:
        df[COL_MED] = np.nan

    # Seleziona/riordina le colonne principali
    keep = [COL_CARD, COL_ID, COL_LINK, COL_5P, COL_MED]
    df = df[keep].copy()

    # Aggiungi espansione
    df["Espansione"] = espansione

    # Normalizza prezzi
    df[COL_MED] = df[COL_MED].apply(to_float)
    df["Prezzi_Lista"] = df[COL_5P].apply(parse_price_list)

    # Chiave unica stabile
    df["CardKey"] = df["Espansione"].astype(str) + "|" + df[COL_ID].astype(str)

    return df

@st.cache_data(show_spinner=True)
def load_all_data_dynamic(data_dir, expansions_map: dict, index_signature: tuple):
    """
    Carica tutti i file indicati in expansions_map (filename -> label).
    index_signature serve solo a invalidare la cache quando file/size cambiano.
    """
    frames, missing = [], []
    for filename, esp in expansions_map.items():
        path = os.path.join(data_dir, filename)
        if os.path.exists(path):
            try:
                frames.append(load_one_excel(path, esp))
            except Exception as e:
                st.warning(f"Errore caricando '{filename}': {e}")
        else:
            missing.append(filename)
    if not frames:
        return pd.DataFrame(), missing
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["CardKey"])
    return df, missing

# ============== PERSISTENZA PREFERITI (GitHub o locale) ==============
def _gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def read_favorites_from_github():
    token = st.secrets.get("GITHUB_TOKEN", None)
    repo = st.secrets.get("GH_REPO", None)
    branch = st.secrets.get("GH_BRANCH", "main")
    path = st.secrets.get("GH_FAV_PATH", "data/favorites.json")

    if not token or not repo:
        return None, None  # secrets non configurati

    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        r = requests.get(url, headers=_gh_headers(token), timeout=20)
    except Exception as e:
        st.warning(f"GitHub GET errore di rete: {e}")
        return None, None

    if r.status_code == 200:
        data = r.json()
        content_b64 = data.get("content", "")
        sha = data.get("sha", "")
        try:
            decoded = base64.b64decode(content_b64).decode("utf-8")
            obj = json.loads(decoded)
        except Exception:
            obj = {"users": {}}
        return obj, sha
    elif r.status_code == 404:
        return {"users": {}}, None  # file non esiste ancora
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

    try:
        r = requests.put(url, headers=_gh_headers(token), json=payload, timeout=20)
    except Exception as e:
        st.warning(f"GitHub PUT errore di rete: {e}")
        return False

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
        except Exception:
            return {"users": {}}
    return {"users": {}}

def write_favorites_local(new_obj):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOCAL_FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(new_obj, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.warning(f"Scrittura preferiti locale fallita: {e}")
        return False

def load_user_favorites(username):
    obj, sha = read_favorites_from_github()
    if obj is not None:
        users = obj.get("users", {})
        arr = users.get(username, [])
        return set(arr), ("github", obj, sha)
    # fallback locale
    obj = read_favorites_local()
    users = obj.get("users", {})
    arr = users.get(username, [])
    return set(arr), ("local", obj, None)

def save_user_favorites(username, favorites_set, backend):
    backend_name, obj, sha = backend
    if obj is None:
        obj = {"users": {}}
    users = obj.get("users", {})
    users[username] = sorted(list(favorites_set))
    obj["users"] = users

    if backend_name == "github":
        return write_favorites_to_github(obj, old_sha=sha, msg=f"update favorites for {username}")
    else:
        return write_favorites_local(obj)

# ============== UI ==============
st.title(APP_TITLE)

# Scopri dinamicamente i dataset disponibili
exp_map, file_list, index_sig = discover_expansions(DATA_DIR)

# Sidebar: Utente + Diagnostica
with st.sidebar:
    st.header("👤 Utente")
    username = st.text_input(
        "Nome utente (serve per ricaricare i tuoi Preferiti)",
        value=st.session_state.get("username", "")
    ).strip()
    if username:
        st.session_state["username"] = username

    st.markdown("---")
    st.header("🧪 Diagnostica")
    secrets_ok = bool(st.secrets.get("GITHUB_TOKEN", None) and st.secrets.get("GH_REPO", None))
    if secrets_ok:
        st.success("Persistenza Preferiti: GitHub ✅")
        st.caption(
            f"Repo: {st.secrets.get('GH_REPO')} | Branch: {st.secrets.get('GH_BRANCH', 'main')} | Path: {st.secrets.get('GH_FAV_PATH', 'data/favorites.json')}"
        )
    else:
        st.info("Persistenza Preferiti: locale/temporanea ⚠️ (configura i Secrets per GitHub)")

    st.markdown("---")
    st.header("📁 Dataset trovati")
    if file_list:
        for f in file_list:
            st.write(f"• `{f}` → **{exp_map.get(f)}**")
    else:
        st.warning("Nessun `.xlsx` trovato nella cartella `data/`.")

    if st.button("🔄 Ricarica dati / clear cache"):
        st.cache_data.clear()
        st.rerun()

# Caricamento dati (dinamico)
with st.spinner("Caricamento dati..."):
    df, missing = load_all_data_dynamic(DATA_DIR, exp_map, index_sig)

if missing:
    st.warning(
        "File indicati ma non trovati in `data/`:\n- " + "\n- ".join(missing) +
        "\nVerifica i nomi dei file e la posizione."
    )

if df.empty:
    st.stop()

# Filtri
with st.sidebar:
    st.markdown("---")
    st.header("🔎 Filtri")
    espansioni = sorted(df["Espansione"].unique())
    sel_esp = st.multiselect("Espansioni", espansioni, default=espansioni)
    query = st.text_input("Cerca per nome carta (parziale):", value="")
    sort_by = st.selectbox("Ordina per", [COL_MED, COL_CARD, "Espansione"], index=0)
    ascending = st.checkbox("Ordine crescente", value=True)
    show_only_favs = st.checkbox("Mostra solo Preferiti ⭐", value=False)

# Applica filtri
work = df[df["Espansione"].isin(sel_esp)].copy()
if query.strip():
    q = query.strip().lower()
    work = work[work[COL_CARD].astype(str).str.lower().str.contains(q)]

# Ordina
if sort_by in work.columns:
    work = work.sort_values(by=sort_by, ascending=ascending, kind="mergesort")

# Carica preferiti per utente
if not username:
    st.info("Inserisci un nome utente nella sidebar per abilitare i Preferiti.")
    user_favs = set()
    backend = ("local", {"users": {}}, None)
else:
    user_favs, backend = load_user_favorites(username)

work = work.assign(Preferito=work["CardKey"].isin(user_favs))

# ===== Tabella principale =====
st.subheader("📄 Carte")
st.caption("Modifica la colonna ⭐Preferito per aggiungere/rimuovere carte ai tuoi preferiti.")

def url_or_empty(u: str):
    u = str(u) if not pd.isna(u) else ""
    return u if u.startswith("http") else ""

view = work[["Espansione", COL_CARD, COL_ID, COL_LINK, COL_MED, "Prezzi_Lista", "Preferito", "CardKey"]].copy()
view["Cardmarket"] = view[COL_LINK].apply(url_or_empty)
view = view.drop(columns=[COL_LINK])

display_cols = ["Espansione", COL_CARD, COL_ID, "Cardmarket", COL_MED, "Prezzi_Lista", "Preferito", "CardKey"]

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
    width="stretch",          # <-- sostituisce use_container_width=True
    height=520,
    key="cards_editor",
)

# Sincronizza preferiti con bottone di salvataggio
if username:
    edited_favs = set(edited.loc[edited["Preferito"] == True, "CardKey"].tolist())
    if edited_favs != user_favs:
        st.info("Hai modificato i preferiti nella tabella. Premi **Salva preferiti** per confermare.")

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("💾 Salva preferiti", type="primary"):
            ok = save_user_favorites(username, edited_favs, backend)
            if ok:
                st.success("Preferiti salvati!")
                st.rerun()
            else:
                st.error("Errore durante il salvataggio dei preferiti.")
    with col2:
        dl_obj = {"users": {username: sorted(list(edited_favs))}}
        st.download_button(
            "⬇️ Esporta preferiti",
            data=json.dumps(dl_obj, ensure_ascii=False, indent=2),
            file_name=f"preferiti_{username}.json",
            mime="application/json",
        )
    with col3:
        up = st.file_uploader("⬆️ Importa preferiti (JSON)", type=["json"], label_visibility="visible")
        if up is not None:
            try:
                imported = json.load(up)
                arr = imported.get("users", {}).get(username, [])
                merged = set(arr).union(edited_favs)
                ok = save_user_favorites(username, merged, backend)
                if ok:
                    st.success("Preferiti importati e salvati!")
                    st.rerun()
                else:
                    st.error("Errore salvataggio dopo import.")
            except Exception as e:
                st.error(f"File JSON non valido: {e}")

# ===== Griglia anteprime =====
st.markdown("---")
st.subheader("📊 Anteprime con mini-grafico (ultimi 5 prezzi)")

MAX_CARDS = 200
preview = work if not st.session_state.get("show_only_favs_override", False) else work[work["Preferito"]]
if show_only_favs:
    preview = work[work["Preferito"]]
preview = preview.head(MAX_CARDS)

cols = st.columns(3)
for i, (_, row) in enumerate(preview.iterrows()):
    with cols[i % 3]:
        box = st.container(border=True)
        with box:
            st.markdown(f"**{row[COL_CARD]}**  \n*{row['Espansione']}*")
            url = url_or_empty(row.get(COL_LINK, ""))
            if url:
                st.markdown(url, help="Apri la pagina su Cardmarket")
            med = row.get(COL_MED, np.nan)
            if pd.notna(med):
                st.metric("Prezzo medio (€)", f"{med:.2f}")
            else:
                st.write("Prezzo medio: n/d")

            prices = row.get("Prezzi_Lista", [])
            if prices:
                st.line_chart(prices, height=100)
                st.caption("Ultimi 5 prezzi: " + ", ".join(f"{p:.2f}€" for p in prices))
            else:
                st.caption("Nessun dato prezzi recenti")

            # Toggle preferito (non scrive subito su GitHub; serve il bottone sopra)
            key = row["CardKey"]
            is_fav = key in user_favs
            new_val = st.toggle("⭐ Preferito", value=is_fav, key=f"fav_{key}")
            if username and new_val != is_fav:
                if new_val:
                    user_favs.add(key)
                else:
                    user_favs.discard(key)
                st.session_state["show_only_favs_override"] = show_only_favs
                st.info("Hai cambiato un preferito in anteprima. Premi **Salva preferiti** sopra per confermare.")
