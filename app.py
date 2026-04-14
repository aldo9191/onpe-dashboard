#!/usr/bin/env python3
"""Dashboard web para proyección electoral ONPE 2026."""

from flask import Flask, jsonify, render_template_string
import requests
import time
import os
import numpy as np
import pandas as pd
from datetime import datetime
from threading import Lock, Thread

app = Flask(__name__)

BASE = "https://resultadoelectoral.onpe.gob.pe/presentacion-backend"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://resultadoelectoral.onpe.gob.pe/main/presidenciales"
}

cache = {"data": None, "timestamp": 0}
cache_lock = Lock()
CACHE_TTL = int(os.environ.get("CACHE_TTL", 120))  # 2 minutes default
MC_REFRESH = int(os.environ.get("MC_REFRESH", 3600))  # 60 min default

def get_json(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                return r.json().get("data", {})
        except:
            if i < retries - 1:
                time.sleep(1)
    return None


def fetch_projection():
    """Fetch fresh data from ONPE and compute projection."""

    # 1. National totals
    totales = get_json(f"{BASE}/resumen-general/totales?idEleccion=10&tipoFiltro=eleccion")
    if not totales:
        return None

    ts = datetime.fromtimestamp(totales["fechaActualizacion"] / 1000)

    # 2. Get departments + extranjero
    deptos = get_json(f"{BASE}/ubigeos/departamentos?idEleccion=10&idAmbitoGeografico=1")
    if not deptos:
        return None

    all_votos = []
    all_meta = []

    for d in deptos:
        ubigeo = d["ubigeo"]
        nombre = d["nombre"]
        ubigeo_num = str(int(ubigeo[:2])) + "0000"

        votos = get_json(f"{BASE}/eleccion-presidencial/participantes-ubicacion-geografica-nombre"
                         f"?tipoFiltro=ubigeo_nivel_01&idAmbitoGeografico=1&ubigeoNivel1={ubigeo_num}&idEleccion=10")
        tot = get_json(f"{BASE}/resumen-general/totales?idAmbitoGeografico=1&idEleccion=10"
                       f"&tipoFiltro=ubigeo_nivel_01&idUbigeoDepartamento={ubigeo_num}")
        parti = get_json(f"{BASE}/participacion-ciudadana/totales?idAmbitoGeografico=1"
                         f"&tipoFiltro=ubigeo_nivel_01&ubigeoNivel01={ubigeo_num}")

        if votos:
            for rec in votos:
                rec["departamento"] = nombre
                all_votos.append(rec)

        meta = {"departamento": nombre, "ubigeo": ubigeo}
        if tot:
            meta["pct_actas"] = tot.get("actasContabilizadas", 0)
            meta["votos_validos"] = tot.get("totalVotosValidos", 0)
        if parti:
            meta["electores_habiles"] = parti.get("totalElectoresHabiles", 0)
        else:
            meta["electores_habiles"] = 0
        all_meta.append(meta)
        time.sleep(0.2)

    # Extranjero
    votos_ext = get_json(f"{BASE}/eleccion-presidencial/participantes-ubicacion-geografica-nombre"
                         f"?tipoFiltro=ambito_geografico&idAmbitoGeografico=2&idEleccion=10")
    tot_ext = get_json(f"{BASE}/resumen-general/totales?idAmbitoGeografico=2&idEleccion=10&tipoFiltro=ambito_geografico")
    parti_ext = get_json(f"{BASE}/participacion-ciudadana/totales?idAmbitoGeografico=2&tipoFiltro=ambito_geografico")

    if votos_ext:
        for rec in votos_ext:
            rec["departamento"] = "EXTRANJERO"
            all_votos.append(rec)

    meta_ext = {"departamento": "EXTRANJERO", "ubigeo": "999999"}
    if tot_ext:
        meta_ext["pct_actas"] = tot_ext.get("actasContabilizadas", 0)
        meta_ext["votos_validos"] = tot_ext.get("totalVotosValidos", 0)
    if parti_ext:
        meta_ext["electores_habiles"] = parti_ext.get("totalElectoresHabiles", 0)
    all_meta.append(meta_ext)

    # 3. Compute projection (bottom-up by padron)
    df_meta = pd.DataFrame(all_meta)
    total_eh = df_meta["electores_habiles"].sum()
    df_meta["peso"] = df_meta["electores_habiles"] / total_eh

    results = {}
    for v in all_votos:
        partido = v["nombreAgrupacionPolitica"]
        candidato = v.get("nombreCandidato", "")
        if partido in ("VOTOS EN BLANCO", "VOTOS NULOS"):
            continue

        key = (partido, candidato)
        depto = v["departamento"]
        pct = v.get("porcentajeVotosValidos")
        votos_val = v.get("totalVotosValidos", 0)

        meta_row = df_meta[df_meta["departamento"] == depto]
        if meta_row.empty:
            continue
        peso = meta_row.iloc[0]["peso"]

        if key not in results:
            results[key] = {"pct": 0, "votos": 0, "codigo": v.get("codigoAgrupacionPolitica", ""),
                            "dni": v.get("dniCandidato", "")}
        if pct is not None:
            results[key]["pct"] += pct * peso
        results[key]["votos"] += votos_val

    # Estimate total valid votes at 100%
    total_vv_est = 0
    for _, m in df_meta.iterrows():
        pct_a = m.get("pct_actas", 0)
        if pct_a > 0:
            total_vv_est += m.get("votos_validos", 0) / pct_a * 100

    # Build output
    total_act = sum(r["votos"] for r in results.values())
    candidatos = []
    for (partido, candidato), vals in results.items():
        pct_actual = vals["votos"] / total_act * 100 if total_act > 0 else 0
        votos_proy = round(total_vv_est * vals["pct"] / 100)
        candidatos.append({
            "partido": partido,
            "candidato": candidato,
            "codigo": vals["codigo"],
            "dni": vals["dni"],
            "votos_actuales": vals["votos"],
            "votos_proyectados": votos_proy,
            "pct_actual": round(pct_actual, 3),
            "pct_proyectado": round(vals["pct"], 3),
            "diferencia": round(vals["pct"] - pct_actual, 3),
        })

    candidatos.sort(key=lambda x: x["pct_proyectado"], reverse=True)

    # Monte Carlo simulation (vectorized, top 5 only)
    # Uses INTRA-DEPARTMENT std from district-level analysis (pre-calculated)
    # This reflects the real uncertainty of pending actas within each department
    INTRA_DEPT_STD = {
        "FUERZA POPULAR": 6.759,
        "RENOVACIÓN POPULAR": 3.869,
        "PARTIDO DEL BUEN GOBIERNO": 3.492,
        "JUNTOS POR EL PERÚ": 12.245,
        "PARTIDO CÍVICO OBRAS": 3.842,
        "PARTIDO PAÍS PARA TODOS": 2.794,
        "AHORA NACIÓN - AN": 3.185,
        "PRIMERO LA GENTE – COMUNIDAD, ECOLOGÍA, LIBERTAD Y PROGRESO": 1.353,
        "PARTIDO SICREO": 1.239,
        "PARTIDO FRENTE DE LA ESPERANZA 2021": 0.878,
    }

    top5_partidos = [c["partido"] for c in candidatos[:5]]
    N_SIMS = 5000

    depto_names = df_meta["departamento"].values
    n_deptos = len(depto_names)
    n_top5 = 5

    pct_matrix = np.zeros((n_deptos, n_top5))
    weight_vec = np.zeros(n_deptos)
    uncert_vec = np.zeros(n_deptos)

    for i, depto in enumerate(depto_names):
        meta_row = df_meta[df_meta["departamento"] == depto].iloc[0]
        weight_vec[i] = meta_row["peso"]
        pct_actas = meta_row.get("pct_actas", 100)
        uncert_vec[i] = max(0, (100 - pct_actas) / 100)

        for j, partido in enumerate(top5_partidos):
            matching = [v for v in all_votos
                        if v["departamento"] == depto and v["nombreAgrupacionPolitica"] == partido]
            if matching:
                pct_val = matching[0].get("porcentajeVotosValidos")
                pct_matrix[i, j] = pct_val if pct_val is not None else 0

    # Use pre-calculated intra-department std from district analysis
    std_vec = np.array([INTRA_DEPT_STD.get(p, 2.0) for p in top5_partidos])

    mc_pcts = np.zeros((N_SIMS, n_top5))
    for sim in range(N_SIMS):
        noise = np.random.randn(n_deptos, n_top5) * std_vec[np.newaxis, :] * uncert_vec[:, np.newaxis]
        sim_pct = np.maximum(0, pct_matrix + noise)
        # No normalization: weighted sum of % already gives national %
        mc_pcts[sim, :] = (sim_pct * weight_vec[:, np.newaxis]).sum(axis=0)

    mc_data = []
    for j in range(n_top5):
        sims = mc_pcts[:, j]
        mc_data.append({
            "partido": top5_partidos[j],
            "candidato": candidatos[j]["candidato"],
            "mean": round(float(np.mean(sims)), 2),
            "p5": round(float(np.percentile(sims, 5)), 2),
            "p25": round(float(np.percentile(sims, 25)), 2),
            "p75": round(float(np.percentile(sims, 75)), 2),
            "p95": round(float(np.percentile(sims, 95)), 2),
            "pct_actual": candidatos[j]["pct_actual"],
        })

    # Department detail
    deptos_detail = []
    for _, m in df_meta.sort_values("pct_actas" if "pct_actas" in df_meta.columns else "departamento").iterrows():
        deptos_detail.append({
            "departamento": m["departamento"],
            "pct_actas": m.get("pct_actas", 0),
            "electores_habiles": int(m.get("electores_habiles", 0)),
            "peso": round(m.get("peso", 0) * 100, 3),
        })

    return {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "pct_actas": totales["actasContabilizadas"],
        "actas_contabilizadas": totales["contabilizadas"],
        "total_actas": totales["totalActas"],
        "total_votos_validos": totales["totalVotosValidos"],
        "candidatos": candidatos,
        "monte_carlo": mc_data,
        "departamentos": deptos_detail,
        "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def background_refresh():
    """Background thread that refreshes cache periodically."""
    while True:
        try:
            data = fetch_projection()
            if data:
                with cache_lock:
                    cache["data"] = data
                    cache["timestamp"] = time.time()
                print(f"[BG] Refresh OK: {data['pct_actas']}% actas | {data['fetch_time']}")
        except Exception as e:
            print(f"[BG] Refresh error: {e}")
        time.sleep(MC_REFRESH)


@app.route("/api/proyeccion")
def api_proyeccion():
    now = time.time()
    with cache_lock:
        if cache["data"] and (now - cache["timestamp"]) < CACHE_TTL:
            return jsonify(cache["data"])

    data = fetch_projection()
    if data:
        with cache_lock:
            cache["data"] = data
            cache["timestamp"] = now
        return jsonify(data)
    return jsonify({"error": "No se pudo obtener datos"}), 500


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Proyeccion Electoral ONPE 2026</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0f1923; color: #e0e0e0; }

        .header {
            background: linear-gradient(135deg, #1a2a3a 0%, #0d1b2a 100%);
            padding: 20px 30px;
            border-bottom: 2px solid #e63946;
            display: flex; justify-content: space-between; align-items: center;
        }
        .header h1 { font-size: 1.5em; color: #fff; }
        .header h1 span { color: #e63946; }
        .header-info { text-align: right; font-size: 0.85em; color: #8899aa; }
        .header-info .actas { font-size: 1.4em; color: #4fc3f7; font-weight: bold; }

        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

        .stats-bar {
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px;
            margin-bottom: 25px;
        }
        .stat-card {
            background: #1a2a3a; border-radius: 10px; padding: 18px;
            border-left: 4px solid #4fc3f7; text-align: center;
        }
        .stat-card .value { font-size: 1.8em; font-weight: bold; color: #fff; }
        .stat-card .label { font-size: 0.8em; color: #8899aa; margin-top: 5px; }

        .main-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
            margin-bottom: 25px;
        }

        .card {
            background: #1a2a3a; border-radius: 10px; padding: 20px;
            border: 1px solid #2a3a4a;
        }
        .card h2 { font-size: 1.1em; color: #4fc3f7; margin-bottom: 15px; border-bottom: 1px solid #2a3a4a; padding-bottom: 8px; }

        .candidate-row {
            display: grid; grid-template-columns: 30px 1fr 110px 110px 80px 80px 70px;
            align-items: center; padding: 10px 8px; border-bottom: 1px solid #1d2d3d;
            transition: background 0.2s;
        }
        .candidate-row:hover { background: #1d2d3d; }
        .candidate-row .pos { font-size: 1.3em; font-weight: bold; color: #4fc3f7; }
        .candidate-row .name { font-weight: 600; color: #fff; font-size: 0.9em; }
        .candidate-row .party { font-size: 0.75em; color: #8899aa; }
        .candidate-row .votes { text-align: right; font-size: 0.85em; color: #ccc; }
        .candidate-row .pct { text-align: right; font-weight: bold; font-size: 1em; }
        .candidate-row .diff { text-align: right; font-size: 0.85em; font-weight: bold; }
        .diff.positive { color: #4caf50; }
        .diff.negative { color: #ef5350; }

        .bar-container { width: 100%; background: #0d1b2a; border-radius: 4px; height: 8px; margin-top: 4px; }
        .bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }

        .progress-bar-outer {
            width: 100%; background: #0d1b2a; border-radius: 8px; height: 24px;
            margin: 10px 0; overflow: hidden; position: relative;
        }
        .progress-bar-inner {
            height: 100%; border-radius: 8px;
            background: linear-gradient(90deg, #e63946, #ff6b6b);
            transition: width 1s ease;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.8em; font-weight: bold; color: #fff;
        }

        .dept-table { width: 100%; font-size: 0.8em; }
        .dept-table th { text-align: left; padding: 6px; color: #4fc3f7; border-bottom: 1px solid #2a3a4a; }
        .dept-table td { padding: 5px 6px; border-bottom: 1px solid #1d2d3d; }
        .dept-table tr:hover { background: #1d2d3d; }

        .chart-container { position: relative; height: 350px; }

        .refresh-btn {
            background: #e63946; color: #fff; border: none; padding: 8px 16px;
            border-radius: 6px; cursor: pointer; font-size: 0.85em; font-weight: 600;
        }
        .refresh-btn:hover { background: #c62828; }
        .refresh-btn:disabled { background: #555; cursor: wait; }

        .auto-refresh { display: flex; align-items: center; gap: 10px; }
        .auto-refresh label { font-size: 0.85em; color: #8899aa; }

        .loading { text-align: center; padding: 60px; color: #4fc3f7; font-size: 1.2em; }
        .loading::after { content: ''; animation: dots 1.5s steps(3) infinite; }
        @keyframes dots { 0% { content: '.'; } 33% { content: '..'; } 66% { content: '...'; } }

        .metodologia {
            background: #1a2a3a; border-radius: 10px; padding: 15px 20px;
            margin-top: 20px; border: 1px solid #2a3a4a; font-size: 0.8em; color: #8899aa;
        }
        .metodologia strong { color: #4fc3f7; }

        .full-width { grid-column: 1 / -1; }

        @media (max-width: 900px) {
            .main-grid { grid-template-columns: 1fr; }
            .stats-bar { grid-template-columns: repeat(2, 1fr); }
            .candidate-row { grid-template-columns: 25px 1fr 90px 90px 65px 65px 55px; font-size: 0.85em; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Proyeccion <span>Electoral</span> ONPE 2026</h1>
            <div style="font-size:0.8em;color:#8899aa;margin-top:4px;">Bottom-Up por Departamento | Ponderado por Padron Electoral</div>
        </div>
        <div class="header-info">
            <div class="actas" id="pctActas">--</div>
            <div>actas contabilizadas</div>
            <div style="margin-top:6px;">
                <span id="timestamp">--</span>
            </div>
            <div style="margin-top:8px;" class="auto-refresh">
                <button class="refresh-btn" onclick="loadData()" id="refreshBtn">Actualizar</button>
                <label><input type="checkbox" id="autoRefresh" checked> Auto (2 min)</label>
            </div>
        </div>
    </div>

    <div class="container">
        <div class="stats-bar">
            <div class="stat-card">
                <div class="value" id="statActas">--</div>
                <div class="label">Actas contabilizadas</div>
            </div>
            <div class="stat-card" style="border-color:#e63946;">
                <div class="value" id="statTotal">--</div>
                <div class="label">Total de actas</div>
            </div>
            <div class="stat-card" style="border-color:#4caf50;">
                <div class="value" id="statVotos">--</div>
                <div class="label">Votos validos</div>
            </div>
            <div class="stat-card" style="border-color:#ff9800;">
                <div class="value" id="statCandidatos">--</div>
                <div class="label">Candidatos</div>
            </div>
        </div>

        <div class="progress-bar-outer">
            <div class="progress-bar-inner" id="progressBar" style="width:0%">0%</div>
        </div>

        <div class="main-grid">
            <div class="card">
                <h2>Ranking Proyectado al 100%</h2>
                <div id="candidateList" class="loading">Cargando datos</div>
            </div>

            <div class="card">
                <h2>% Proyectado vs Actual</h2>
                <div class="chart-container">
                    <canvas id="chartComparison"></canvas>
                </div>
            </div>

            <div class="card">
                <h2>Cambio Proyectado (puntos porcentuales)</h2>
                <div class="chart-container">
                    <canvas id="chartDiff"></canvas>
                </div>
            </div>

            <div class="card">
                <h2>Monte Carlo - Intervalos de Confianza (5,000 sims)</h2>
                <div class="chart-container">
                    <canvas id="chartMonteCarlo"></canvas>
                </div>
            </div>

            <div class="card">
                <h2>Proyeccion Promedio (Monte Carlo)</h2>
                <div id="mcTable"></div>
            </div>

            <div class="card">
                <h2>Avance de Actas por Departamento</h2>
                <div style="max-height:350px;overflow-y:auto;">
                    <table class="dept-table" id="deptTable">
                        <thead><tr><th>Departamento</th><th>% Actas</th><th>Electores</th><th>Peso</th></tr></thead>
                        <tbody id="deptBody"></tbody>
                    </table>
                </div>
            </div>

        </div>

        <div class="metodologia">
            <strong>Metodologia:</strong> Proyeccion bottom-up ponderada por padron electoral.
            Para cada departamento se calcula el % de votos validos por candidato (dato observado) y se pondera por el peso
            del departamento segun sus electores habiles en el padron electoral. Incluye voto del extranjero.
            Los datos se actualizan automaticamente cada 2 minutos desde la API de la ONPE.
        </div>
    </div>

    <script>
    const COLORS = [
        '#e63946', '#4fc3f7', '#4caf50', '#ff9800', '#9c27b0',
        '#00bcd4', '#ff5722', '#8bc34a', '#ffc107', '#3f51b5',
        '#e91e63', '#009688', '#ff6f00', '#7c4dff', '#00e676'
    ];

    let chartComparison = null;
    let chartDiff = null;
    let chartMC = null;
    let autoRefreshInterval = null;

    function formatNumber(n) {
        return n.toLocaleString('es-PE');
    }

    function shortName(candidato) {
        if (!candidato) return '';
        const parts = candidato.split(' ');
        if (parts.length >= 2) {
            return parts[parts.length - 2] + ' ' + parts[parts.length - 1];
        }
        return candidato;
    }

    async function loadData() {
        const btn = document.getElementById('refreshBtn');
        btn.disabled = true;
        btn.textContent = 'Cargando...';

        try {
            const resp = await fetch('/api/proyeccion');
            const data = await resp.json();

            if (data.error) {
                document.getElementById('candidateList').innerHTML = '<div style="color:#ef5350;">Error: ' + data.error + '</div>';
                return;
            }

            renderData(data);
        } catch (e) {
            console.error(e);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Actualizar';
        }
    }

    function renderData(data) {
        // Header
        document.getElementById('pctActas').textContent = data.pct_actas.toFixed(2) + '%';
        document.getElementById('timestamp').textContent = 'ONPE: ' + data.timestamp + ' | Consulta: ' + data.fetch_time;

        // Stats
        document.getElementById('statActas').textContent = formatNumber(data.actas_contabilizadas);
        document.getElementById('statTotal').textContent = formatNumber(data.total_actas);
        document.getElementById('statVotos').textContent = formatNumber(data.total_votos_validos);
        document.getElementById('statCandidatos').textContent = data.candidatos.length;

        // Progress bar
        const pbar = document.getElementById('progressBar');
        pbar.style.width = data.pct_actas + '%';
        pbar.textContent = data.pct_actas.toFixed(2) + '%';

        // Candidate list - TOP 5 only
        const top5 = data.candidatos.slice(0, 5);
        let html = `
            <div class="candidate-row" style="border-bottom:2px solid #2a3a4a;padding-bottom:6px;margin-bottom:4px;">
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600">#</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600">Candidato / Partido</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600;text-align:right">Votos Reales</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600;text-align:right">Votos Proy.</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600;text-align:right">% Actual</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600;text-align:right">% Proy.</div>
                <div style="font-size:0.75em;color:#4fc3f7;font-weight:600;text-align:right">Dif.</div>
            </div>`;
        top5.forEach((c, i) => {
            const diffClass = c.diferencia >= 0 ? 'positive' : 'negative';
            const diffSign = c.diferencia >= 0 ? '+' : '';
            const barWidth = (c.pct_proyectado / top5[0].pct_proyectado * 100);
            html += `
                <div class="candidate-row">
                    <div class="pos">${i + 1}</div>
                    <div>
                        <div class="name">${c.candidato || c.partido}</div>
                        <div class="party">${c.partido}</div>
                        <div class="bar-container"><div class="bar-fill" style="width:${barWidth}%;background:${COLORS[i % COLORS.length]}"></div></div>
                    </div>
                    <div class="votes">${formatNumber(c.votos_actuales)}</div>
                    <div class="votes">${formatNumber(c.votos_proyectados)}</div>
                    <div class="pct-actual" style="color:#8899aa;text-align:right;font-size:0.9em">${c.pct_actual.toFixed(2)}%</div>
                    <div class="pct" style="color:${COLORS[i % COLORS.length]}">${c.pct_proyectado.toFixed(2)}%</div>
                    <div class="diff ${diffClass}">${diffSign}${c.diferencia.toFixed(2)}</div>
                </div>`;
        });
        document.getElementById('candidateList').innerHTML = html;

        // Charts - TOP 5 only
        const chartTop5 = data.candidatos.slice(0, 5);
        const labels = chartTop5.map(c => shortName(c.candidato));

        // Chart 1: Comparison bars
        if (chartComparison) chartComparison.destroy();
        chartComparison = new Chart(document.getElementById('chartComparison'), {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { label: '% Actual', data: chartTop5.map(c => c.pct_actual), backgroundColor: COLORS.slice(0,5).map(c => c + 'CC'), borderWidth: 0 },
                    { label: '% Proyectado', data: chartTop5.map(c => c.pct_proyectado), backgroundColor: COLORS.slice(0,5).map(c => c + '66'), borderColor: COLORS.slice(0,5), borderWidth: 2 }
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#ccc' } } },
                scales: {
                    x: { ticks: { color: '#999', font: { size: 10 } }, grid: { color: '#1d2d3d' } },
                    y: { ticks: { color: '#999', callback: v => v + '%' }, grid: { color: '#1d2d3d' } }
                }
            }
        });

        // Chart 2: Difference
        if (chartDiff) chartDiff.destroy();
        chartDiff = new Chart(document.getElementById('chartDiff'), {
            type: 'bar',
            data: {
                labels: chartTop5.map(c => shortName(c.candidato)),
                datasets: [{
                    label: 'Diferencia (pp)',
                    data: chartTop5.map(c => c.diferencia),
                    backgroundColor: chartTop5.map(c => c.diferencia >= 0 ? '#4caf50AA' : '#ef5350AA'),
                    borderColor: chartTop5.map(c => c.diferencia >= 0 ? '#4caf50' : '#ef5350'),
                    borderWidth: 2
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: '#999', callback: v => (v >= 0 ? '+' : '') + v.toFixed(2) + ' pp' }, grid: { color: '#1d2d3d' } },
                    y: { ticks: { color: '#ccc', font: { size: 11 } }, grid: { display: false } }
                }
            }
        });

        // Chart 3: Monte Carlo intervals
        if (chartMC) chartMC.destroy();
        if (data.monte_carlo && data.monte_carlo.length > 0) {
            const mcLabels = data.monte_carlo.map(m => shortName(m.candidato));
            const mcColors = COLORS.slice(0, data.monte_carlo.length);

            // Floating bars: [min, max] for 90% CI and 50% CI
            const ci90_data = data.monte_carlo.map(m => [m.p5, m.p95]);
            const ci50_data = data.monte_carlo.map(m => [m.p25, m.p75]);
            const meanData = data.monte_carlo.map(m => m.mean);
            const actualData = data.monte_carlo.map(m => m.pct_actual);

            chartMC = new Chart(document.getElementById('chartMonteCarlo'), {
                type: 'bar',
                data: {
                    labels: mcLabels,
                    datasets: [
                        {
                            label: 'IC 90% (P5-P95)',
                            data: ci90_data,
                            backgroundColor: mcColors.map(c => c + '33'),
                            borderColor: mcColors.map(c => c + '88'),
                            borderWidth: 1,
                            barPercentage: 0.6,
                        },
                        {
                            label: 'IC 50% (P25-P75)',
                            data: ci50_data,
                            backgroundColor: mcColors.map(c => c + '77'),
                            borderColor: mcColors,
                            borderWidth: 1.5,
                            barPercentage: 0.4,
                        },
                        {
                            label: 'Media MC',
                            data: meanData,
                            type: 'line',
                            pointBackgroundColor: mcColors,
                            pointBorderColor: '#fff',
                            pointBorderWidth: 2,
                            pointRadius: 7,
                            showLine: false,
                        },
                        {
                            label: '% Actual',
                            data: actualData,
                            type: 'line',
                            pointBackgroundColor: '#fff',
                            pointBorderColor: '#888',
                            pointBorderWidth: 2,
                            pointRadius: 5,
                            pointStyle: 'crossRot',
                            showLine: false,
                        }
                    ]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { labels: { color: '#ccc', font: { size: 10 } } },
                        tooltip: {
                            callbacks: {
                                label: function(ctx) {
                                    const mc = data.monte_carlo[ctx.dataIndex];
                                    if (ctx.datasetIndex === 0) return `IC 90%: ${mc.p5}% - ${mc.p95}%`;
                                    if (ctx.datasetIndex === 1) return `IC 50%: ${mc.p25}% - ${mc.p75}%`;
                                    if (ctx.datasetIndex === 2) return `Media MC: ${mc.mean}%`;
                                    return `Actual: ${mc.pct_actual}%`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            ticks: { color: '#999', callback: v => v.toFixed(1) + '%' },
                            grid: { color: '#1d2d3d' },
                            title: { display: true, text: '% Votos Validos', color: '#888' }
                        },
                        y: { ticks: { color: '#ccc', font: { size: 11 } }, grid: { display: false } }
                    }
                }
            });
        }

        // Monte Carlo table
        if (data.monte_carlo && data.monte_carlo.length > 0) {
            let mctHtml = `
                <table class="dept-table" style="font-size:0.85em;">
                    <thead><tr>
                        <th>Candidato</th>
                        <th style="text-align:right">% Actual</th>
                        <th style="text-align:right">% Proy.</th>
                        <th style="text-align:right">MC Mean</th>
                        <th style="text-align:right">IC 90%</th>
                    </tr></thead><tbody>`;
            data.monte_carlo.forEach((m, i) => {
                const c = data.candidatos[i];
                const diffMC = m.mean - m.pct_actual;
                const diffClass = diffMC >= 0 ? 'positive' : 'negative';
                const diffSign = diffMC >= 0 ? '+' : '';
                mctHtml += `<tr>
                    <td style="font-weight:600;color:${COLORS[i]}">${shortName(m.candidato)}</td>
                    <td style="text-align:right;color:#8899aa">${m.pct_actual.toFixed(2)}%</td>
                    <td style="text-align:right">${c.pct_proyectado.toFixed(2)}%</td>
                    <td style="text-align:right;font-weight:bold;color:${COLORS[i]}">${m.mean.toFixed(2)}%</td>
                    <td style="text-align:right;font-size:0.9em;color:#8899aa">[${m.p5.toFixed(2)} - ${m.p95.toFixed(2)}]</td>
                </tr>`;
            });
            mctHtml += '</tbody></table>';
            document.getElementById('mcTable').innerHTML = mctHtml;
        }

        // Department table
        const depts = data.departamentos.sort((a, b) => a.pct_actas - b.pct_actas);
        let dhtml = '';
        depts.forEach(d => {
            const color = d.pct_actas > 80 ? '#4caf50' : d.pct_actas > 60 ? '#ff9800' : '#ef5350';
            dhtml += `<tr>
                <td>${d.departamento}</td>
                <td><span style="color:${color};font-weight:bold">${d.pct_actas.toFixed(1)}%</span></td>
                <td style="text-align:right">${formatNumber(d.electores_habiles)}</td>
                <td style="text-align:right">${d.peso.toFixed(2)}%</td>
            </tr>`;
        });
        document.getElementById('deptBody').innerHTML = dhtml;
    }

    // Auto-refresh
    document.getElementById('autoRefresh').addEventListener('change', function() {
        if (this.checked) {
            autoRefreshInterval = setInterval(loadData, 120000);
        } else {
            clearInterval(autoRefreshInterval);
        }
    });

    // Initial load
    loadData();
    autoRefreshInterval = setInterval(loadData, 120000);
    </script>
</body>
</html>
"""

# Start background refresh thread (works with both flask dev and gunicorn)
_bg_started = False

def ensure_background_thread():
    global _bg_started
    if not _bg_started:
        _bg_started = True
        bg = Thread(target=background_refresh, daemon=True)
        bg.start()
        print(f"[BG] Background refresh every {MC_REFRESH}s started")

# Auto-start on import (for gunicorn)
ensure_background_thread()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print("=" * 60)
    print(f"  Dashboard Electoral ONPE 2026")
    print(f"  http://localhost:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
