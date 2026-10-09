import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from datetime import datetime, timezone, timedelta
import io
import json
import os
import time
import base64
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from PIL import Image

# ============================================================
# CONFIGURACIÓN Y ESTILOS
# ============================================================
st.set_page_config(page_title="Kora | Recicla y Gana", page_icon="♻️", layout="centered")

st.markdown("""
<style>
    .stApp { background: linear-gradient(180deg, #f4fff6 0%, #ffffff 55%); }
    .card { background: white; padding: 25px; border-radius: 20px; box-shadow: 0px 8px 25px rgba(0,0,0,0.07); margin-bottom: 20px; border: 1px solid #e5eee7; }
    .success-box { background: #e9f8ed; border: 2px solid #43a047; padding: 22px; border-radius: 18px; text-align: center; margin-top: 20px; }
    .error-box { background: #fff0f0; border: 2px solid #e05252; padding: 22px; border-radius: 18px; text-align: center; margin-top: 20px; }
    .stButton > button { width: 100%; border-radius: 14px; min-height: 50px; background-color: #176b3a; color: white; font-weight: 700; border: none; font-size: 16px; }
    .stButton > button:hover { background-color: #0f512b; color: white; }
    .logo { text-align: center; font-size: 42px; font-weight: 800; color: #176b3a; margin-top: 10px; margin-bottom: 0px; }
    .subtitle { text-align: center; color: #68736b; font-size: 16px; margin-bottom: 30px; }
    .points-box { background: linear-gradient(135deg, #176b3a, #2e8b57); padding: 25px; border-radius: 20px; text-align: center; color: white; margin-bottom: 20px; }
    .points-number { font-size: 42px; font-weight: 800; }
</style>
""", unsafe_allow_html=True)

# Evita que el traductor automático del navegador rompa la página
components.html(
    """
    <script>
    const d = window.parent.document;
    d.documentElement.setAttribute('translate', 'no');
    d.documentElement.classList.add('notranslate');
    if (!d.querySelector('meta[name="google"]')) {
        const m = d.createElement('meta');
        m.name = 'google';
        m.content = 'notranslate';
        d.head.appendChild(m);
    }
    </script>
    """,
    height=0,
)

ARCHIVO_DATOS = "datos_kora.csv"   # solo se usa si no hay Supabase configurado
COLUMNAS = ["Carnet", "Botellas", "Puntos", "Fecha", "Resultado"]
# Se prueban en orden. Si un nombre no existe (404), su cuota se llenó (429)
# o tarda demasiado, pasa al siguiente.
MODELOS_IA = [
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
]
SEGUNDOS_ENTRE_INTENTOS = 5
TIMEOUT_POR_MODELO = 25      # segundos que se espera a cada modelo
TIEMPO_MAXIMO_TOTAL = 75     # segundos máximos por foto en total
HORA_GUATEMALA = timezone(timedelta(hours=-6))


# ============================================================
# SECRETOS
# ============================================================
def secreto(nombre, defecto=""):
    """Lee un valor desde Secrets (Streamlit Cloud / secrets.toml) o variables de entorno."""
    try:
        valor = st.secrets[nombre]
        if valor:
            return str(valor)
    except Exception:
        pass
    return os.environ.get(nombre, defecto)


# ============================================================
# DATOS (Supabase si está configurado; si no, un archivo local)
# ============================================================
def supabase_activo():
    return bool(secreto("SUPABASE_URL") and secreto("SUPABASE_KEY"))


def sb_request(metodo, ruta, cuerpo=None):
    """Llamada simple a la API de Supabase, sin librerías extra."""
    url = secreto("SUPABASE_URL").strip().rstrip("/") + "/rest/v1/" + ruta
    clave = secreto("SUPABASE_KEY").strip()
    headers = {"apikey": clave, "Content-Type": "application/json"}
    if clave.startswith("eyJ"):  # claves antiguas (service_role) también van en Authorization
        headers["Authorization"] = f"Bearer {clave}"
    if metodo == "POST":
        headers["Prefer"] = "return=minimal"
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, headers=headers, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            texto = r.read().decode("utf-8")
            return json.loads(texto) if texto else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase respondió {e.code}: {e.read().decode()}")


def cargar_datos(carnet=None):
    """Devuelve la tabla de registros. Si se da un carnet, solo los de esa persona."""
    if supabase_activo():
        try:
            ruta = "registros?select=carnet,botellas,puntos,fecha,resultado&order=id.desc&limit=1000"
            if carnet:
                ruta += "&carnet=eq." + urllib.parse.quote(str(carnet).strip(), safe="")
            filas = sb_request("GET", ruta) or []
        except Exception as e:
            st.error("🚨 No se pudo leer la base de datos.")
            with st.expander("Detalle técnico"):
                st.code(str(e))
            return pd.DataFrame(columns=COLUMNAS)
        if not filas:
            return pd.DataFrame(columns=COLUMNAS)
        df = pd.DataFrame(filas).rename(columns={
            "carnet": "Carnet", "botellas": "Botellas", "puntos": "Puntos",
            "fecha": "Fecha", "resultado": "Resultado",
        })
        df["Carnet"] = df["Carnet"].astype(str).str.strip()
        return df[COLUMNAS]

    # Modo archivo local (solo para pruebas en Codespaces)
    if os.path.exists(ARCHIVO_DATOS):
        try:
            df = pd.read_csv(ARCHIVO_DATOS, dtype={"Carnet": str})
            df["Carnet"] = df["Carnet"].astype(str).str.strip()
            if carnet:
                df = df[df["Carnet"] == str(carnet).strip()]
            return df
        except Exception:
            pass
    return pd.DataFrame(columns=COLUMNAS)


def guardar_registro(carnet):
    """Guarda una botella válida. Lanza un error si no se pudo guardar."""
    fila = {
        "carnet": str(carnet).strip(),
        "botellas": 1,
        "puntos": 10,
        "fecha": datetime.now(HORA_GUATEMALA).strftime("%Y-%m-%d %H:%M"),
        "resultado": "Válida",
    }
    if supabase_activo():
        sb_request("POST", "registros", fila)
        return

    df = cargar_datos()
    nuevo = pd.DataFrame([{
        "Carnet": fila["carnet"], "Botellas": fila["botellas"], "Puntos": fila["puntos"],
        "Fecha": fila["fecha"], "Resultado": fila["resultado"],
    }])
    pd.concat([df, nuevo], ignore_index=True).to_csv(ARCHIVO_DATOS, index=False)


@st.cache_resource
def cache_resultados():
    """Guarda resultados de fotos ya analizadas (compartido entre todos los usuarios)."""
    return {}


def reducir_imagen(image_bytes, max_lado=768):
    """Las fotos del celular pesan mucho. Se achican para que la IA responda rápido."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((max_lado, max_lado))
        salida = io.BytesIO()
        img.save(salida, format="JPEG", quality=80)
        return salida.getvalue()
    except Exception:
        return image_bytes


# ============================================================
# CEREBRO IA (CONEXIÓN DIRECTA, SIN LIBRERÍAS EXTRA)
# ============================================================
def analizar_imagen_directo(imagen_camara):
    api_key = secreto("GEMINI_API_KEY")
    if not api_key:
        return None, "Falta la API key. Agrégala en Secrets como GEMINI_API_KEY."

    try:
        image_bytes = imagen_camara.getvalue()

        # Si esta misma foto ya se analizó, no volvemos a gastar cuota
        clave_foto = hashlib.md5(image_bytes).hexdigest()
        cache = cache_resultados()
        if clave_foto in cache:
            return cache[clave_foto], None

        base64_image = base64.b64encode(reducir_imagen(image_bytes)).decode("utf-8")

        data = {
            "contents": [{
                "parts": [
                    {"text": "Eres la visión artificial de una máquina de reciclaje estricta. Tu única tarea es decidir si la foto muestra una BOTELLA DE PLÁSTICO PET. REGLAS: Si ves una taza de cerámica, taza de café, vaso o tarro, RECHÁZALO inmediatamente. Si ves solo manos, brazos o estuches, RECHÁZALO. Responde SOLO en JSON estricto: {\"es_botella_pet\": true o false, \"objeto_detectado\": \"qué objeto ves realmente\", \"razon\": \"por qué lo rechazas o apruebas\"}"},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64_image
                        }
                    }
                ]
            }],
            "generationConfig": {"responseMimeType": "application/json"}
        }

        cuerpo = json.dumps(data).encode("utf-8")
        ultimo_error = "Sin respuesta de la IA."
        codigos = []
        hubo_timeout = False
        inicio = time.time()

        for modelo in MODELOS_IA:
            if time.time() - inicio > TIEMPO_MAXIMO_TOTAL:
                break

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

            for intento in range(2):
                req = urllib.request.Request(
                    url,
                    data=cuerpo,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": api_key.strip(),
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=TIMEOUT_POR_MODELO) as response:
                        result = json.loads(response.read().decode("utf-8"))
                        texto_respuesta = result["candidates"][0]["content"]["parts"][0]["text"]
                        texto_limpio = texto_respuesta.strip().replace("```json", "").replace("```", "")
                        resultado = json.loads(texto_limpio)
                        resultado["_modelo"] = modelo
                        cache[clave_foto] = resultado
                        return resultado, None
                except urllib.error.HTTPError as e:
                    codigos.append(e.code)
                    ultimo_error = f"Error de conexión ({e.code}) con {modelo}: {e.read().decode()}"
                    if e.code in (500, 503, 504) and intento == 0:
                        time.sleep(2)  # servidor saturado: un solo reintento
                        continue
                    break  # 429 (cuota), 404 (nombre no existe), 400, etc.: siguiente modelo
                except (TimeoutError, urllib.error.URLError, OSError) as e:
                    # Google tardó demasiado o se cortó la conexión: siguiente modelo
                    hubo_timeout = True
                    ultimo_error = f"Tiempo agotado o conexión cortada con {modelo}: {e}"
                    break
                except (KeyError, IndexError, json.JSONDecodeError) as e:
                    # El modelo respondió algo que no se pudo leer: siguiente modelo
                    ultimo_error = f"Respuesta inesperada de {modelo}: {e}"
                    break

        if 429 in codigos:
            return None, "CUOTA|" + ultimo_error
        if hubo_timeout:
            return None, "LENTO|" + ultimo_error
        return None, ultimo_error

    except Exception as e:
        return None, f"Error inesperado: {str(e)}"


# ============================================================
# INTERFAZ
# ============================================================
st.markdown('<div class="logo">♻️ KORA</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Recicla • Suma • Transforma</div>', unsafe_allow_html=True)

opcion = st.radio("Navegación", ["♻️ Reciclar", "🏆 Mis puntos", "📊 Administración"], horizontal=True)

if opcion == "♻️ Reciclar":
    carnet = st.text_input("🆔 Número de carnet")
    foto = st.camera_input("Toma la foto")

    if st.button("🔎 Verificar con IA"):
        ahora = time.time()
        espera = SEGUNDOS_ENTRE_INTENTOS - (ahora - st.session_state.get("ultimo_intento", 0))

        if not carnet.strip():
            st.warning("Ingresa tu carnet.")
        elif not foto:
            st.warning("Toma la foto.")
        elif espera > 0:
            st.warning(f"⏳ Espera {int(espera) + 1} segundos antes de intentar de nuevo.")
        else:
            st.session_state["ultimo_intento"] = ahora

            with st.spinner("🤖 Kora está escaneando el objeto..."):
                resultado_ia, error = analizar_imagen_directo(foto)

            if error:
                if error.startswith("CUOTA|"):
                    st.warning("⏳ Kora está recibiendo muchas fotos al mismo tiempo. Espera 1 minuto e intenta de nuevo.")
                    detalle = error.replace("CUOTA|", "", 1)
                elif error.startswith("LENTO|"):
                    st.warning("🐢 La IA tardó demasiado en responder. Intenta de nuevo en unos segundos.")
                    detalle = error.replace("LENTO|", "", 1)
                else:
                    st.error("🚨 Algo salió mal en la comunicación.")
                    detalle = error
                with st.expander("Detalle técnico"):
                    st.code(detalle)
            else:
                es_botella = resultado_ia.get("es_botella_pet", False)
                objeto = resultado_ia.get("objeto_detectado", "")

                if es_botella:
                    try:
                        guardar_registro(carnet)
                    except Exception as e:
                        st.error("🚨 La botella se reconoció, pero no se pudieron guardar los puntos. Intenta de nuevo.")
                        with st.expander("Detalle técnico"):
                            st.code(str(e))
                    else:
                        st.markdown(
                            f'<div class="success-box">🎉 ¡Botella reconocida!<br>Vimos: <b>{objeto}</b><h1>+10 puntos</h1></div>',
                            unsafe_allow_html=True,
                        )
                        st.balloons()
                else:
                    st.markdown(
                        f'<div class="error-box">❌ RECHAZADO<br>Vimos: <b>{objeto}</b><br>Razón: {resultado_ia.get("razon", "")}</div>',
                        unsafe_allow_html=True,
                    )

                st.caption(f"🤖 Analizado con: {resultado_ia.get('_modelo', 'desconocido')}")

elif opcion == "🏆 Mis puntos":
    busqueda = st.text_input("🆔 Ingresa tu carnet")
    if st.button("Consultar"):
        usuario = cargar_datos(carnet=busqueda)
        if not usuario.empty:
            st.markdown(
                f'<div class="points-box">PUNTOS ACUMULADOS<div class="points-number">{usuario["Puntos"].sum()}</div></div>',
                unsafe_allow_html=True,
            )
            st.metric("♻️ Botellas", usuario["Botellas"].sum())
            st.dataframe(usuario[["Fecha", "Botellas", "Puntos"]], hide_index=True, use_container_width=True)
        else:
            st.info("Sin registros para ese carnet.")

elif opcion == "📊 Administración":
    if st.text_input("Contraseña:", type="password") == secreto("ADMIN_PASSWORD", "kora2026"):
        df = cargar_datos()
        col1, col2, col3 = st.columns(3)
        col1.metric("Usuarios", df["Carnet"].nunique())
        col2.metric("Botellas", df["Botellas"].sum())
        col3.metric("Puntos", df["Puntos"].sum())
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption("🗄️ Datos guardados en: " + ("Supabase (nube)" if supabase_activo() else "archivo local (solo pruebas)"))