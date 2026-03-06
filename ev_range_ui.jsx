import { useState, useCallback, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, AreaChart, Area
} from "recharts";

// ─────────────────────────────────────────────────────────────────────────────
// EPA fueleconomy.gov — XML via corsproxy.io (no CORS restriction)
// ─────────────────────────────────────────────────────────────────────────────
const EPA_BASE = "https://corsproxy.io/?https://www.fueleconomy.gov/ws/rest";

async function epaGet(path) {
  const res = await fetch(`${EPA_BASE}${path}`, { headers: { Accept: "application/xml" } });
  if (!res.ok) throw new Error(`EPA API ${res.status}`);
  const xml = await res.text();
  return new DOMParser().parseFromString(xml, "application/xml");
}
function xmlItems(doc, tag = "menuItem") {
  return Array.from(doc.getElementsByTagName(tag)).map(el => ({
    value: el.getElementsByTagName("value")[0]?.textContent || "",
    text:  el.getElementsByTagName("text")[0]?.textContent  || "",
  }));
}
function xmlField(doc, field) {
  return doc.getElementsByTagName(field)[0]?.textContent || "";
}
const fetchYears   = async () => xmlItems(await epaGet("/vehicle/menu/year")).filter(y => +y.value >= 2011).sort((a,b) => b.value - a.value);
const fetchMakes   = async (yr)      => xmlItems(await epaGet(`/vehicle/menu/make?year=${yr}`));
const fetchModels  = async (yr,mk)   => xmlItems(await epaGet(`/vehicle/menu/model?year=${yr}&make=${encodeURIComponent(mk)}`));
const fetchOptions = async (yr,mk,md)=> xmlItems(await epaGet(`/vehicle/menu/options?year=${yr}&make=${encodeURIComponent(mk)}&model=${encodeURIComponent(md)}`));
async function fetchVehicle(id) {
  const doc = await epaGet(`/vehicle/${id}`);
  const g   = f => xmlField(doc, f);
  return {
    id, make:g("make"), model:g("model"), year:g("year"),
    trany:g("trany"), drive:g("drive"), atvType:g("atvType"),
    evMotor:g("evMotor"), sizeClass:g("VClass"), fuelType:g("fuelType1"),
    cityE:    parseFloat(g("cityE"))     || 0,
    hwyE:     parseFloat(g("hwyE"))      || 0,
    combE:    parseFloat(g("combE"))     || 0,
    range:    parseFloat(g("range"))     || 0,
    comb08:   parseFloat(g("comb08"))    || 0,
    charge240:parseFloat(g("charge240")) || 0,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// CONSTANTS
// ─────────────────────────────────────────────────────────────────────────────
const ROAD_TYPES = [
  { id:"1", label:"Highway", speed_range:[90,130], base_speed:110, cr_mult:1.00, regen_factor:0.05 },
  { id:"2", label:"Mixed",   speed_range:[60,110], base_speed:85,  cr_mult:1.10, regen_factor:0.18 },
  { id:"3", label:"City",    speed_range:[25,70],  base_speed:50,  cr_mult:1.25, regen_factor:0.35 },
];
const HVAC_OPTIONS = [
  { id:"off", label:"Off" }, { id:"eco", label:"Eco" }, { id:"comfort", label:"Comfort" },
];

function physicsFromClass(sizeClass) {
  const s = (sizeClass || "").toLowerCase();
  if (s.includes("pickup"))   return { mass:3050,Cd:0.36,A:3.50,Cr:0.012,eta:0.88,usable_DoD:0.86,regen_eff:0.28,aux_w:1800 };
  if (s.includes("van"))      return { mass:3200,Cd:0.38,A:4.20,Cr:0.012,eta:0.88,usable_DoD:0.85,regen_eff:0.28,aux_w:1800 };
  if (s.includes("special purpose")||s.includes("large suv")) return { mass:2650,Cd:0.33,A:3.00,Cr:0.011,eta:0.89,usable_DoD:0.87,regen_eff:0.30,aux_w:1600 };
  if (s.includes("suv")||s.includes("utility")) return { mass:2050,Cd:0.29,A:2.60,Cr:0.010,eta:0.90,usable_DoD:0.88,regen_eff:0.32,aux_w:1300 };
  if (s.includes("large"))    return { mass:1950,Cd:0.26,A:2.35,Cr:0.009,eta:0.91,usable_DoD:0.88,regen_eff:0.34,aux_w:1200 };
  if (s.includes("midsize")||s.includes("mid-size")) return { mass:1900,Cd:0.25,A:2.30,Cr:0.009,eta:0.92,usable_DoD:0.88,regen_eff:0.35,aux_w:1200 };
  return { mass:1700,Cd:0.27,A:2.20,Cr:0.009,eta:0.91,usable_DoD:0.87,regen_eff:0.33,aux_w:1100 };
}
function estimateBattery(v) {
  if (v.combE > 0 && v.range > 0) return Math.round((v.range * v.combE / 100) / 0.88 * 10) / 10;
  return null;
}

// ─────────────────────────────────────────────────────────────────────────────
// PHYSICS ENGINE
// ─────────────────────────────────────────────────────────────────────────────
const G           = 9.81;
const airDensity  = t => 1.225 * (288.15 / (t + 273.15));
const precipMult  = p => ({ none:1.0, rain:1.15, snow:1.30 }[p] || 1.0);
const sohMiles    = mi => mi < 20000 ? 1.0 : mi > 150000 ? 0.75 : 1.0 - ((mi-20000)/130000)*0.25;
const batCapF     = t => t >= 20 && t <= 35 ? 1.0 : t < 20 ? Math.max(0.60, 1-(20-t)*0.015) : Math.max(0.88, 1-(t-35)*0.006);
const hvacW       = (t, mode) => mode === "off" ? 0 : Math.abs(t-22) * (mode === "eco" ? 30 : 65) + (mode === "eco" ? 500 : 1200);

function compute(veh, wx, road, soc, reserve, miles, speed_kph, theta_deg=0) {
  const v   = speed_kph / 3.6;
  const w   = (wx.wind_kph||0) / 3.6;
  const tr  = (theta_deg||0) * Math.PI/180;
  const rho = airDensity(wx.temp_c);
  const cr  = precipMult(wx.precip) * road.cr_mult;

  const f_rr   = veh.Cr * cr * veh.mass * G;
  const f_drag = 0.5 * rho * veh.Cd * veh.A * (v+w)**2;
  const f_grd  = veh.mass * G * Math.sin(tr);
  const prop_w = ((f_rr + f_drag + f_grd) * v) / veh.eta;

  const hvac_w  = hvacW(wx.temp_c, wx.hvac);
  const total_w = prop_w + veh.aux_w + hvac_w;
  const regen   = Math.max(0, prop_w) * road.regen_factor * veh.regen_eff;
  const net_w   = Math.max(0, prop_w - regen) + veh.aux_w + hvac_w;
  let   wh_km   = (net_w / v) / 3600 * 1000;

  // EPA calibration: if we have EPA kWh/100mi, compute a scalar at EPA-test
  // conditions then apply it so physics reproduces the EPA baseline exactly.
  if (veh.epa_wh_km_hwy > 0 || veh.epa_wh_km_city > 0) {
    const frac     = road.id==="1"?0 : road.id==="3"?1 : 0.5;
    const epa_wh   = (1-frac)*(veh.epa_wh_km_hwy||veh.epa_wh_km_city) + frac*(veh.epa_wh_km_city||veh.epa_wh_km_hwy);
    const ev       = road.base_speed/3.6;
    const e_rho    = airDensity(22);
    const e_fRR    = veh.Cr * road.cr_mult * veh.mass * G;
    const e_drag   = 0.5 * e_rho * veh.Cd * veh.A * ev * ev;
    const e_prop   = ((e_fRR + e_drag) * ev) / veh.eta;
    const e_regen  = Math.max(0, e_prop) * road.regen_factor * veh.regen_eff;
    const e_net    = Math.max(0, e_prop - e_regen) + veh.aux_w;
    const phys_wh  = (e_net / ev) / 3600 * 1000;
    const cal      = phys_wh > 0 ? Math.max(0.6, Math.min(1.4, epa_wh / phys_wh)) : 1;
    wh_km *= cal;
  }

  const soh      = sohMiles(miles);
  const cap_f    = batCapF(wx.temp_c);
  const avail    = veh.cap_kwh * soh * cap_f * veh.usable_DoD * Math.max(0, (soc-reserve)/100);
  const range_km = Math.max(0, avail * 1000 / wh_km);

  const b_rho  = airDensity(25);
  const b_fRR  = veh.Cr * road.cr_mult * veh.mass * G;
  const b_drag = 0.5 * b_rho * veh.Cd * veh.A * v * v;
  const b_prop = ((b_fRR + b_drag) * v) / veh.eta;
  const b_reg  = Math.max(0, b_prop) * road.regen_factor * veh.regen_eff;
  const b_net  = Math.max(0, b_prop - b_reg) + veh.aux_w;
  const b_wh   = (b_net / v) / 3600 * 1000;
  const b_av   = veh.cap_kwh * 1.0 * 1.0 * veh.usable_DoD * Math.max(0,(soc-reserve)/100);
  const b_rng  = Math.max(0, b_av * 1000 / b_wh);

  const safeW = Math.max(1, total_w);
  return {
    speed_kph, range_km, range_mi: range_km*0.621371, wh_per_km: wh_km, avail_kwh: avail,
    soh, cap_factor: cap_f, reduction_pct: b_rng > 0 ? Math.max(0,(b_rng-range_km)/b_rng*100) : 0,
    regen_credit_w: regen,
    losses: {
      aero: (f_drag*v/veh.eta)/safeW*100, rolling: (f_rr*v/veh.eta)/safeW*100,
      grade: (Math.abs(f_grd)*v/veh.eta)/safeW*100, hvac: hvac_w/safeW*100,
      base_aux: veh.aux_w/safeW*100, thermal: (1-cap_f)*100,
      regen_recovery: regen/safeW*100,
    },
  };
}

function buildSweep(veh, wx, road, soc, reserve, miles, theta) {
  const [lo,hi] = road.speed_range;
  const results=[]; let best=null;
  for (let s=lo; s<=hi; s+=5) {
    const r = compute(veh, wx, road, soc, reserve, miles, s, theta);
    results.push({ speed:Math.round(s*0.621), range:Math.round(r.range_mi), wh_km:Math.round(r.wh_per_km*10)/10 });
    if (!best || r.range_km > best.range_km) best = { ...r, speed_kph:s };
  }
  return { sweep:results, optimal:best };
}

function buildSOC(veh, wx, road, soc, reserve, miles, speed_kph, theta, dist_km, label) {
  const r    = compute(veh, wx, road, soc, reserve, miles, speed_kph, theta);
  const total= veh.cap_kwh * sohMiles(miles) * batCapF(wx.temp_c) * veh.usable_DoD;
  let   cur  = total * soc/100;
  const flr  = total * reserve/100;
  const maxD = Math.ceil(Math.max(dist_km||0, r.range_km)+5);
  const pts  = [{dist:0, [label]:Math.round(cur/total*100*10)/10}];
  for (let d=1; d<=maxD; d++) {
    cur -= r.wh_per_km/1000;
    pts.push({dist:Math.round(d*0.621*10)/10, [label]:Math.round(Math.max(0,cur/total*100)*10)/10});
    if (cur<=flr) break;
  }
  return pts;
}

// ─────────────────────────────────────────────────────────────────────────────
// GEO / WEATHER / ELEVATION
// ─────────────────────────────────────────────────────────────────────────────
async function geocode(place) {
  const d = await (await fetch(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(place)}&format=json&limit=1`,
    {headers:{"User-Agent":"ev-range-ui/1.0","Accept-Language":"en"}})).json();
  if (!d.length) throw new Error(`Cannot find "${place}"`);
  return { lat:+d[0].lat, lon:+d[0].lon, name:d[0].display_name.split(",")[0] };
}
async function fetchWeather(lat,lon) {
  const c = (await (await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,wind_speed_10m,precipitation,weather_code&wind_speed_unit=kmh&timezone=auto`)).json()).current;
  const wc=c.weather_code;
  let precip="none";
  if (wc>=51&&wc<=67) precip="rain"; else if (wc>=71&&wc<=77) precip="snow";
  if (c.precipitation>0.5&&precip==="none") precip="rain";
  return { temp_c:Math.round(c.temperature_2m*10)/10, wind_kph:Math.round(c.wind_speed_10m), precip };
}
function hav(a,b){
  const R=6371,dL=(b.lat-a.lat)*Math.PI/180,dN=(b.lon-a.lon)*Math.PI/180;
  const x=Math.sin(dL/2)**2+Math.cos(a.lat*Math.PI/180)*Math.cos(b.lat*Math.PI/180)*Math.sin(dN/2)**2;
  return R*2*Math.atan2(Math.sqrt(x),Math.sqrt(1-x));
}
async function fetchElev(orig,dest,n=12){
  const pts=Array.from({length:n},(_,i)=>({lat:orig.lat+i/(n-1)*(dest.lat-orig.lat),lon:orig.lon+i/(n-1)*(dest.lon-orig.lon)}));
  try {
    const data=await(await fetch(`https://api.open-topo-data.com/v1/srtm90m?locations=${pts.map(p=>`${p.lat.toFixed(5)},${p.lon.toFixed(5)}`).join("|")}`)).json();
    const elevs=data.results.map(r=>r.elevation||0);
    const km=hav(orig,dest)*1.25;
    const dp=Array.from({length:n},(_,i)=>i/(n-1)*km);
    return {elevs,dist_pts:dp,deg:Math.atan2(elevs[n-1]-elevs[0],km*1000)*180/Math.PI,
      gain:elevs.reduce((s,e,i)=>i>0?s+Math.max(0,e-elevs[i-1]):s,0),
      loss:elevs.reduce((s,e,i)=>i>0?s+Math.max(0,elevs[i-1]-e):s,0),flat:false};
  } catch {
    const km=hav(orig,dest)*1.25;
    return {elevs:new Array(n).fill(0),dist_pts:Array.from({length:n},(_,i)=>i/(n-1)*km),deg:0,gain:0,loss:0,flat:true};
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// UI PRIMITIVES
// ─────────────────────────────────────────────────────────────────────────────
const C = {
  bg:"#ffffff", panelBg:"#f8fafc", border:"#e2e8f0", borderMid:"#cbd5e1",
  text:"#1e293b", textMid:"#64748b", textMute:"#94a3b8",
  amber:"#d97706", amberLight:"#f59e0b", amberBg:"#fffbeb", amberBorder:"#fbbf24",
  green:"#059669", greenBg:"#f0fdf4", greenBorder:"#bbf7d0",
  blue:"#3b82f6", red:"#ef4444",
};

const iStyle = {
  width:"100%", background:C.bg, border:`1px solid ${C.borderMid}`,
  borderRadius:4, padding:"7px 10px", color:C.text,
  fontFamily:"'Space Mono',monospace", fontSize:13, outline:"none",
  boxSizing:"border-box", transition:"border-color 0.15s",
};

const Lbl = ({children}) => (
  <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,letterSpacing:"0.12em",
    textTransform:"uppercase",color:C.textMute,display:"block",marginBottom:4}}>{children}</span>
);

const Field = ({label,value,onChange,type="text",placeholder="",min,max,step,unit,ro}) => (
  <div style={{marginBottom:11}}>
    <Lbl>{label}{unit&&<span style={{color:C.textMute}}> ({unit})</span>}</Lbl>
    <input type={type} value={value} min={min} max={max} step={step} placeholder={placeholder}
      readOnly={!!ro} onChange={e=>!ro&&onChange(e.target.value)}
      style={{...iStyle,background:ro?"#f8fafc":C.bg}}
      onFocus={e=>{if(!ro)e.target.style.borderColor=C.amberLight;}}
      onBlur={e=>e.target.style.borderColor=C.borderMid} />
  </div>
);

const Drop = ({label,value,onChange,options,disabled}) => (
  <div style={{marginBottom:11}}>
    <Lbl>{label}</Lbl>
    <select value={value} onChange={e=>onChange(e.target.value)} disabled={!!disabled}
      style={{...iStyle,fontSize:12,cursor:disabled?"not-allowed":"pointer",opacity:disabled?.5:1}}
      onFocus={e=>e.target.style.borderColor=C.amberLight}
      onBlur={e=>e.target.style.borderColor=C.borderMid}>
      {options.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  </div>
);

const SecHead = ({n,label}) => (
  <div style={{fontSize:9,letterSpacing:"0.15em",textTransform:"uppercase",
    color:C.amber,margin:"20px 0 12px",paddingBottom:7,borderBottom:`1px solid ${C.border}`}}>
    {n} / {label}
  </div>
);

const Spin = () => (
  <>
    <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    <span style={{display:"inline-block",width:12,height:12,border:`2px solid ${C.border}`,
      borderTopColor:C.amber,borderRadius:"50%",animation:"spin 0.7s linear infinite",
      verticalAlign:"middle",marginLeft:6}} />
  </>
);

const Tag = ({children,color="#065f46",bg="#f0fdf4",border="#bbf7d0"}) => (
  <span style={{fontSize:9,letterSpacing:"0.08em",textTransform:"uppercase",
    background:bg,border:`1px solid ${border}`,color,
    padding:"3px 8px",borderRadius:20,fontFamily:"'Space Mono',monospace"}}>{children}</span>
);

const ChartTip = ({active,payload,label,unit=""}) => {
  if(!active||!payload?.length) return null;
  return (
    <div style={{background:C.bg,border:`1px solid ${C.border}`,padding:"8px 12px",
      borderRadius:4,fontFamily:"'Space Mono',monospace",fontSize:11,
      boxShadow:"0 4px 12px rgba(0,0,0,0.08)"}}>
      <div style={{color:C.textMute,marginBottom:4}}>{label}{unit}</div>
      {payload.map((p,i)=><div key={i} style={{color:p.color||C.text}}>{p.name}: <b>{p.value}{unit}</b></div>)}
    </div>
  );
};

const LossBar = ({label,pct,color,isReturn}) => (
  <div style={{marginBottom:7}}>
    <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
      <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,letterSpacing:"0.06em",
        color:isReturn?C.green:C.textMid}}>{label}</span>
      <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,
        color:isReturn?C.green:C.text}}>{isReturn?"+":""}{pct.toFixed(1)}%</span>
    </div>
    <div style={{height:4,background:C.border,borderRadius:2,overflow:"hidden"}}>
      <div style={{height:"100%",width:`${Math.min(100,pct)}%`,
        background:isReturn?C.green:color,borderRadius:2,transition:"width 0.5s ease"}} />
    </div>
  </div>
);

const StatCard = ({label,value,sub,accent}) => (
  <div style={{background:C.bg,border:`1px solid ${accent?"#fca5a5":C.border}`,borderRadius:6,
    padding:"12px 14px",boxShadow:"0 1px 3px rgba(0,0,0,0.04)",
    background: accent ? "rgba(220,38,38,0.04)" : C.bg}}>
    <div style={{fontFamily:"'Space Mono',monospace",fontSize:10,letterSpacing:"0.1em",
      textTransform:"uppercase",color:accent?"#b91c1c":C.textMute,marginBottom:4}}>{label}</div>
    <div style={{fontFamily:"'Space Mono',monospace",fontSize:15,color:accent?"#b91c1c":C.text}}>
      {value}
      {sub&&<span style={{fontSize:11,color:accent?"#fca5a5":C.textMute,marginLeft:6}}>{sub}</span>}
    </div>
  </div>
);

// ─────────────────────────────────────────────────────────────────────────────
// EPA VEHICLE PICKER
// ─────────────────────────────────────────────────────────────────────────────
function EPAPicker({ onSelect }) {
  const [years,  setYears]  = useState([]);
  const [makes,  setMakes]  = useState([]);
  const [models, setModels] = useState([]);
  const [trims,  setTrims]  = useState([]);
  const [yr,setYr]=useState(""); const [mk,setMk]=useState(""); const [md,setMd]=useState(""); const [tr,setTr]=useState("");
  const [busy,setBusy]=useState({yr:false,mk:false,md:false,tr:false,veh:false});
  const [card,setCard]=useState(null);
  const [err,setErr]=useState(null);

  const loading = k => setBusy(b=>({...b,[k]:true}));
  const done    = k => setBusy(b=>({...b,[k]:false}));

  useEffect(()=>{
    loading("yr");
    fetchYears().then(y=>{setYears(y);if(y.length)setYr(y[0].value);}).catch(e=>setErr(e.message)).finally(()=>done("yr"));
  },[]);

  useEffect(()=>{
    if(!yr) return;
    setMk("");setMd("");setTr("");setMakes([]);setModels([]);setTrims([]);setCard(null);
    loading("mk");
    fetchMakes(yr).then(setMakes).catch(e=>setErr(e.message)).finally(()=>done("mk"));
  },[yr]);

  useEffect(()=>{
    if(!yr||!mk) return;
    setMd("");setTr("");setModels([]);setTrims([]);setCard(null);
    loading("md");
    fetchModels(yr,mk).then(setModels).catch(e=>setErr(e.message)).finally(()=>done("md"));
  },[yr,mk]);

  useEffect(()=>{
    if(!yr||!mk||!md) return;
    setTr("");setTrims([]);setCard(null);
    loading("tr");
    fetchOptions(yr,mk,md).then(setTrims).catch(e=>setErr(e.message)).finally(()=>done("tr"));
  },[yr,mk,md]);

  useEffect(()=>{
    if(!tr) return;
    setCard(null); setErr(null);
    loading("veh");
    fetchVehicle(tr).then(v=>{setCard(v);onSelect(v);}).catch(e=>setErr(e.message)).finally(()=>done("veh"));
  },[tr]);

  const isEV = card && (card.atvType?.toLowerCase().includes("ev")||card.combE>0||card.fuelType?.toLowerCase().includes("electric"));

  const dropStyle = (disabled) => ({...iStyle,fontSize:12,cursor:disabled?"not-allowed":"pointer",opacity:disabled?.5:1});

  return (
    <div>
      {err && (
        <div style={{padding:"8px 10px",background:"#fff1f2",border:"1px solid #fecdd3",
          borderRadius:4,fontSize:10,color:"#e11d48",marginBottom:10,lineHeight:1.6}}>
          ⚠ {err} — EPA API may be temporarily unavailable.
        </div>
      )}

      {/* Year */}
      <div style={{marginBottom:10}}>
        <Lbl>Model Year {busy.yr&&<Spin/>}</Lbl>
        <select value={yr} onChange={e=>setYr(e.target.value)} disabled={busy.yr} style={dropStyle(busy.yr)}
          onFocus={e=>e.target.style.borderColor=C.amberLight} onBlur={e=>e.target.style.borderColor=C.borderMid}>
          {!yr&&<option value="">Select year…</option>}
          {years.map(y=><option key={y.value} value={y.value}>{y.text}</option>)}
        </select>
      </div>

      {/* Make */}
      {yr && (
        <div style={{marginBottom:10}}>
          <Lbl>Make {busy.mk&&<Spin/>}</Lbl>
          <select value={mk} onChange={e=>setMk(e.target.value)} disabled={busy.mk||!makes.length} style={dropStyle(busy.mk||!makes.length)}
            onFocus={e=>e.target.style.borderColor=C.amberLight} onBlur={e=>e.target.style.borderColor=C.borderMid}>
            <option value="">{busy.mk?"Loading…":"Select make…"}</option>
            {makes.map(m=><option key={m.value} value={m.value}>{m.text}</option>)}
          </select>
        </div>
      )}

      {/* Model */}
      {mk && (
        <div style={{marginBottom:10}}>
          <Lbl>Model {busy.md&&<Spin/>}</Lbl>
          <select value={md} onChange={e=>setMd(e.target.value)} disabled={busy.md||!models.length} style={dropStyle(busy.md||!models.length)}
            onFocus={e=>e.target.style.borderColor=C.amberLight} onBlur={e=>e.target.style.borderColor=C.borderMid}>
            <option value="">{busy.md?"Loading…":"Select model…"}</option>
            {models.map(m=><option key={m.value} value={m.value}>{m.text}</option>)}
          </select>
        </div>
      )}

      {/* Trim */}
      {md && (
        <div style={{marginBottom:10}}>
          <Lbl>Trim {busy.tr&&<Spin/>}</Lbl>
          <select value={tr} onChange={e=>setTr(e.target.value)} disabled={busy.tr||!trims.length} style={dropStyle(busy.tr||!trims.length)}
            onFocus={e=>e.target.style.borderColor=C.amberLight} onBlur={e=>e.target.style.borderColor=C.borderMid}>
            <option value="">{busy.tr?"Loading…":"Select trim…"}</option>
            {trims.map(t=><option key={t.value} value={t.value}>{t.text}</option>)}
          </select>
        </div>
      )}

      {busy.veh && <div style={{fontSize:11,color:C.textMid,padding:"6px 0"}}>Fetching EPA data… <Spin/></div>}

      {/* EPA Card */}
      {card && (
        <div style={{background:isEV?C.greenBg:C.amberBg,border:`1px solid ${isEV?C.greenBorder:C.amberBorder}`,
          borderRadius:6,padding:"12px 14px",marginBottom:10}}>
          <div style={{fontFamily:"'Space Mono',monospace",fontSize:10,fontWeight:700,letterSpacing:"0.08em",
            color:isEV?"#065f46":"#92400e",marginBottom:10}}>
            {isEV?"✓  BEV — EPA Data Loaded":"⚠  Not a pure EV — data may be limited"}
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:"6px 14px"}}>
            {[
              ["EPA Range",       card.range>0    ? `${card.range} mi`       : "—"],
              ["City kWh/100mi",  card.cityE>0    ? `${card.cityE}`          : "—"],
              ["Hwy kWh/100mi",   card.hwyE>0     ? `${card.hwyE}`           : "—"],
              ["MPGe",            card.comb08>0   ? `${card.comb08}`         : "—"],
              ["Drive",           card.drive      || "—"],
              ["240V charge",     card.charge240>0? `${card.charge240} hr`   : "—"],
            ].map(([k,v])=>(
              <div key={k}>
                <div style={{fontSize:9,color:C.textMute,letterSpacing:"0.08em",textTransform:"uppercase"}}>{k}</div>
                <div style={{fontFamily:"'Space Mono',monospace",fontSize:12,color:C.text}}>{v}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────────────────────────────────────
const DEF_PHY = {mass:"1900",Cd:"0.24",A:"2.25",Cr:"0.008",eta:"0.92",DoD:"0.88",regen:"0.35",aux:"1200"};

export default function App() {
  const [origin,setOrigin]=useState("New York, NY");
  const [dest,setDest]=useState("Boston, MA");
  const [roadId,setRoadId]=useState("1");
  const [epaVeh,setEpaVeh]=useState(null);
  const [capKwh,setCapKwh]=useState("");
  const [adv,setAdv]=useState(DEF_PHY);
  const setA = (k,v) => setAdv(p=>({...p,[k]:v}));
  const [showAdv,setShowAdv]=useState(false);
  const [soc,setSoc]=useState("85");
  const [res,setRes]=useState("10");
  const [miles,setMiles]=useState("30000");
  const [hvac,setHvac]=useState("eco");
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState(null);
  const [results,setResults]=useState(null);
  const [tab,setTab]=useState("range");

  const handleEpa = useCallback(v => {
    setEpaVeh(v);
    const est = estimateBattery(v);
    if (est) setCapKwh(String(est));
    const p = physicsFromClass(v.sizeClass);
    setAdv({mass:String(p.mass),Cd:String(p.Cd),A:String(p.A),Cr:String(p.Cr),
            eta:String(p.eta),DoD:String(p.usable_DoD),regen:String(p.regen_eff),aux:String(p.aux_w)});
  },[]);

  const compute_ = useCallback(async () => {
    if (!epaVeh) { setError("Please select a vehicle from the EPA lookup."); return; }
    if (!capKwh || +capKwh <= 0) { setError("Please enter battery capacity."); return; }
    setLoading(true); setError(null); setResults(null);
    try {
      const [origGeo,destGeo] = await Promise.all([geocode(origin),geocode(dest)]);
      const [wx,elev]         = await Promise.all([fetchWeather(origGeo.lat,origGeo.lon),fetchElev(origGeo,destGeo)]);
      wx.hvac = hvac;
      const distKm = Math.round(hav(origGeo,destGeo)*1.25);
      const road   = ROAD_TYPES.find(r=>r.id===roadId);

      const veh = {
        name:`${epaVeh.year} ${epaVeh.make} ${epaVeh.model}`,
        cap_kwh:+capKwh, mass:+adv.mass, Cd:+adv.Cd, A:+adv.A, Cr:+adv.Cr,
        eta:+adv.eta, usable_DoD:+adv.DoD, regen_eff:+adv.regen, aux_w:+adv.aux,
        epa_wh_km_city: epaVeh.cityE>0 ? epaVeh.cityE*10/16.0934 : 0,
        epa_wh_km_hwy:  epaVeh.hwyE>0  ? epaVeh.hwyE*10/16.0934  : 0,
        epa_range_mi: epaVeh.range, epa_mpge: epaVeh.comb08,
      };

      const socN=+soc, resN=+res, miN=+miles, theta=elev.deg;
      const {sweep,optimal} = buildSweep(veh,wx,road,socN,resN,miN,theta);
      const baseR = compute(veh,wx,road,socN,resN,miN,road.base_speed,theta);

      const mergeSOC=(a,b,kA,kB)=>{
        const m={};
        a.forEach(p=>{m[p.dist]={...m[p.dist]||{},dist:p.dist,[kA]:p[kA]};});
        b.forEach(p=>{m[p.dist]={...m[p.dist]||{},dist:p.dist,[kB]:p[kB]};});
        return Object.values(m).sort((x,y)=>x.dist-y.dist);
      };
      const bL=`Base ${road.base_speed}`, oL=`Optimal ${Math.round(optimal.speed_kph*0.621)} mph`;
      const hB=buildSOC(veh,wx,road,socN,resN,miN,road.base_speed,theta,distKm,bL);
      const hO=buildSOC(veh,wx,road,socN,resN,miN,optimal.speed_kph,theta,distKm,oL);
      const socD=mergeSOC(hB,hO,bL,oL);
      const elevD=elev.dist_pts.map((d,i)=>({dist:Math.round(d*0.621),elev:Math.round(elev.elevs[i]*3.281)}));

      setResults({origGeo,destGeo,distKm,wx,elev,road,veh,ev:epaVeh,
        optimal,baseR,sweep,socD,elevD,bL,oL,resN,socN,theta});
      setTab("range");
    } catch(e) { setError(e.message); }
    finally    { setLoading(false);   }
  },[origin,dest,roadId,epaVeh,capKwh,soc,res,miles,hvac,adv]);

  const PRECIP={none:"Clear",rain:"Rain",snow:"Snow"};

  return (
    <>
      <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Serif+Display&display=swap" rel="stylesheet"/>
      <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}} *{box-sizing:border-box;}`}</style>

      <div style={{minHeight:"100vh",background:C.bg,
        backgroundImage:"radial-gradient(ellipse at 20% 50%,#fef9ee 0%,transparent 60%),radial-gradient(ellipse at 80% 20%,#f0f9ff 0%,transparent 50%)",
        display:"flex",flexDirection:"column",fontFamily:"'Space Mono',monospace",color:C.text}}>

        {/* HEADER */}
        <div style={{borderBottom:`1px solid ${C.border}`,padding:"14px 28px",
          display:"flex",alignItems:"center",gap:16,
          background:"rgba(255,255,255,0.92)",backdropFilter:"blur(8px)",
          position:"sticky",top:0,zIndex:100}}>
          <div style={{width:32,height:32,borderRadius:6,
            background:"linear-gradient(135deg,#f59e0b,#d97706)",
            display:"flex",alignItems:"center",justifyContent:"center",fontSize:16,flexShrink:0}}>⚡</div>
          <div>
            <div style={{fontFamily:"'DM Serif Display',serif",fontSize:18,color:"#0f172a"}}>EV Range Simulator</div>
            <div style={{fontSize:9,color:C.textMute,letterSpacing:"0.12em",textTransform:"uppercase"}}>
              EPA Data · Live Weather · Physics-Based
            </div>
          </div>
          <div style={{marginLeft:"auto",display:"flex",gap:18}}>
            {["EPA FuelEconomy.gov","Open-Meteo","Open-Topo-Data","Nominatim"].map(s=>(
              <span key={s} style={{fontSize:9,color:C.textMute,letterSpacing:"0.07em",textTransform:"uppercase",
                borderLeft:`1px solid ${C.border}`,paddingLeft:14}}>{s}</span>
            ))}
          </div>
        </div>

        {/* BODY */}
        <div style={{display:"flex",flex:1,overflow:"hidden"}}>

          {/* LEFT PANEL */}
          <div style={{width:320,flexShrink:0,overflowY:"auto",
            borderRight:`1px solid ${C.border}`,background:C.panelBg,padding:"16px 18px"}}>

            <SecHead n="01" label="Route"/>
            <Field label="Origin"      value={origin} onChange={setOrigin} placeholder="City, State"/>
            <Field label="Destination" value={dest}   onChange={setDest}   placeholder="City, State"/>
            <Drop  label="Road type"   value={roadId} onChange={setRoadId}
              options={ROAD_TYPES.map(r=>({value:r.id,label:r.label}))}/>

            <SecHead n="02" label="Vehicle — EPA Lookup"/>
            <div style={{fontSize:10,color:C.textMid,marginBottom:12,lineHeight:1.6}}>
              Search all EPA-tested EVs. City/highway kWh/100mi data calibrates the physics engine.
            </div>
            <EPAPicker onSelect={handleEpa}/>

            <Field label={epaVeh?`Battery capacity (auto-estimated: ${capKwh} kWh)`:"Battery capacity (kWh)"}
              value={capKwh} onChange={setCapKwh} type="number" min="10" max="300" step="0.5"/>

            <button onClick={()=>setShowAdv(v=>!v)}
              style={{background:"none",border:`1px solid ${C.borderMid}`,borderRadius:4,
                color:C.textMid,fontFamily:"'Space Mono',monospace",fontSize:10,
                letterSpacing:"0.1em",textTransform:"uppercase",cursor:"pointer",
                padding:"6px 12px",width:"100%",marginBottom:12,transition:"all 0.15s"}}
              onMouseEnter={e=>{e.target.style.borderColor=C.amber;e.target.style.color=C.amber;}}
              onMouseLeave={e=>{e.target.style.borderColor=C.borderMid;e.target.style.color=C.textMid;}}>
              {showAdv?"▲ Hide":"▼ Advanced"} physics params
            </button>
            {showAdv && (
              <div style={{borderLeft:`2px solid ${C.border}`,paddingLeft:12,marginBottom:8}}>
                <Field label="Mass (kg)"           value={adv.mass}  onChange={v=>setA("mass",v)}  type="number"/>
                <Field label="Drag coeff (Cd)"     value={adv.Cd}    onChange={v=>setA("Cd",v)}    type="number" step="0.01"/>
                <Field label="Frontal area (m²)"   value={adv.A}     onChange={v=>setA("A",v)}     type="number" step="0.1"/>
                <Field label="Rolling resist (Cr)" value={adv.Cr}    onChange={v=>setA("Cr",v)}    type="number" step="0.001"/>
                <Field label="Drivetrain eff"       value={adv.eta}   onChange={v=>setA("eta",v)}   type="number" step="0.01"/>
                <Field label="Usable DoD"           value={adv.DoD}   onChange={v=>setA("DoD",v)}   type="number" step="0.01"/>
                <Field label="Regen efficiency"     value={adv.regen} onChange={v=>setA("regen",v)} type="number" step="0.01"/>
                <Field label="Base aux load (W)"    value={adv.aux}   onChange={v=>setA("aux",v)}   type="number"/>
              </div>
            )}

            <SecHead n="03" label="Conditions"/>
            <Field label="Battery level" value={soc}   onChange={setSoc}   type="number" min="5"  max="100" unit="%"/>
            <Field label="Reserve SOC"   value={res}   onChange={setRes}   type="number" min="0"  max="30"  unit="%"/>
            <Field label="Odometer"      value={miles} onChange={setMiles} type="number" min="0"            unit="miles"/>
            <Drop  label="Climate"       value={hvac}  onChange={setHvac}
              options={HVAC_OPTIONS.map(o=>({value:o.id,label:o.label}))}/>

            <button onClick={compute_} disabled={loading}
              style={{width:"100%",marginTop:18,
                background:loading?"#f1f5f9":"linear-gradient(135deg,#f59e0b,#d97706)",
                border:"none",borderRadius:6,padding:14,
                fontFamily:"'Space Mono',monospace",fontSize:12,fontWeight:700,
                letterSpacing:"0.12em",textTransform:"uppercase",
                color:loading?C.textMute:"#ffffff",
                cursor:loading?"not-allowed":"pointer",transition:"all 0.2s"}}>
              {loading?"COMPUTING…":"COMPUTE RANGE →"}
            </button>

            {error && (
              <div style={{marginTop:10,padding:10,background:"#fff1f2",
                border:"1px solid #fecdd3",borderRadius:4,fontSize:11,color:"#e11d48",lineHeight:1.6}}>
                {error}
              </div>
            )}
          </div>

          {/* RIGHT PANEL */}
          <div style={{flex:1,overflowY:"auto",padding:"24px 28px"}}>

            {!results && !loading && (
              <div style={{display:"flex",flexDirection:"column",alignItems:"center",
                justifyContent:"center",height:"80%",gap:16,opacity:.4}}>
                <div style={{fontSize:48}}>⚡</div>
                <div style={{fontFamily:"'DM Serif Display',serif",fontSize:22,color:C.textMid}}>
                  Select a vehicle and compute
                </div>
                <div style={{fontSize:11,color:C.textMute,textAlign:"center",maxWidth:340,lineHeight:1.7}}>
                  Vehicle data from EPA fueleconomy.gov<br/>
                  Weather from Open-Meteo · Elevation from SRTM 90m<br/>
                  EPA kWh/100mi calibrates the physics engine
                </div>
              </div>
            )}

            {loading && (
              <div style={{display:"flex",flexDirection:"column",alignItems:"center",
                justifyContent:"center",height:"80%",gap:12}}>
                <div style={{fontSize:11,color:C.amber,letterSpacing:"0.14em",
                  textTransform:"uppercase",animation:"pulse 1.5s infinite"}}>
                  Fetching weather, elevation & computing…
                </div>
              </div>
            )}

            {results && (()=>{
              const {origGeo,destGeo,distKm,wx,elev,road,veh,ev,
                     optimal,baseR,sweep,socD,elevD,bL,oL,resN} = results;
              const o=optimal, r=baseR;
              const canReach=r.range_km>=distKm, optReach=o.range_km>=distKm;
              const gainMi=Math.round((o.range_km-r.range_km)*0.621);
              const pctOfEpa=ev.range>0 ? ((o.range_mi/ev.range)*100).toFixed(0) : null;

              // Arrival SOC: subtract energy used for dist_km from initial SOC
              // total usable capacity = cap_kwh × soh × cap_factor × DoD
              const totalUsable = veh.cap_kwh * r.soh * r.cap_factor * veh.usable_DoD;
              const energyUsed_kwh = distKm * r.wh_per_km / 1000;
              const energyUsed_opt = distKm * o.wh_per_km / 1000;
              const arrivalSoc     = totalUsable > 0
                ? Math.max(0, (veh.cap_kwh * r.soh * r.cap_factor * veh.usable_DoD * (socN/100) - energyUsed_kwh) / totalUsable * 100)
                : 0;
              const arrivalSocOpt  = totalUsable > 0
                ? Math.max(0, (veh.cap_kwh * o.soh * o.cap_factor * veh.usable_DoD * (socN/100) - energyUsed_opt) / totalUsable * 100)
                : 0;
              const arrivalSocPct     = Math.round(arrivalSoc * 10) / 10;
              const arrivalSocOptPct  = Math.round(arrivalSocOpt * 10) / 10;
              const aboveReserve      = Math.round((arrivalSocPct - resN) * 10) / 10;
              const aboveReserveOpt   = Math.round((arrivalSocOptPct - resN) * 10) / 10;

              return (
                <div>
                  {/* Route header */}
                  <div style={{marginBottom:18}}>
                    <div style={{fontFamily:"'DM Serif Display',serif",fontSize:20,color:"#0f172a",marginBottom:4}}>
                      {origGeo.name} → {destGeo.name}
                    </div>
                    <div style={{fontSize:10,color:C.textMid,display:"flex",gap:12,flexWrap:"wrap"}}>
                      <span>{Math.round(distKm*0.621)} mi</span><span>·</span>
                      <span>{wx.temp_c}°C</span><span>·</span>
                      <span>{Math.round(wx.wind_kph*0.621)} mph wind</span><span>·</span>
                      <span>{PRECIP[wx.precip]}</span>
                      {!elev.flat&&<><span>·</span><span>Grade {elev.deg>=0?"+":""}{elev.deg.toFixed(2)}°</span></>}
                      <span>·</span><span style={{color:C.amber,fontWeight:700}}>{veh.name}</span>
                    </div>
                  </div>

                  {/* EPA badges */}
                  {(ev.cityE>0||ev.hwyE>0) && (
                    <div style={{display:"flex",gap:8,marginBottom:16,flexWrap:"wrap"}}>
                      {ev.cityE>0&&<Tag>EPA City: {ev.cityE} kWh/100mi</Tag>}
                      {ev.hwyE>0&&<Tag>EPA Hwy: {ev.hwyE} kWh/100mi</Tag>}
                      {ev.comb08>0&&<Tag>{ev.comb08} MPGe</Tag>}
                      {ev.range>0&&<Tag>Rated {ev.range} mi</Tag>}
                      <Tag color="#1d4ed8" bg="#eff6ff" border="#bfdbfe">⚙ Physics calibrated</Tag>
                    </div>
                  )}

                  {/* HERO */}
                  <div style={{background:"linear-gradient(135deg,#fffbeb,#fef3c7)",
                    border:"1px solid #fbbf24",borderRadius:8,padding:"22px 26px",
                    marginBottom:18,position:"relative",overflow:"hidden"}}>
                    <div style={{position:"absolute",top:-40,right:-40,width:200,height:200,borderRadius:"50%",
                      background:"radial-gradient(circle,rgba(251,191,36,.25) 0%,transparent 70%)",pointerEvents:"none"}}/>
                    <div style={{fontSize:9,letterSpacing:"0.18em",textTransform:"uppercase",color:"#b45309",marginBottom:8}}>
                      ★  Optimal Cruise Speed
                    </div>
                    <div style={{display:"flex",alignItems:"flex-end",gap:12,flexWrap:"wrap"}}>
                      <div style={{fontFamily:"'DM Serif Display',serif",fontSize:72,lineHeight:1,
                        color:C.amber,textShadow:"0 0 24px rgba(217,119,6,0.25)",fontWeight:400}}>
                        {Math.round(o.speed_kph*0.621)}
                      </div>
                      <div style={{paddingBottom:10}}>
                        <div style={{fontFamily:"'Space Mono',monospace",fontSize:18,color:"#78716c",letterSpacing:"0.05em"}}>mph</div>
                        <div style={{fontFamily:"'Space Mono',monospace",fontSize:11,color:"#a8a29e"}}>({o.speed_kph} km/h)</div>
                      </div>
                      <div style={{paddingBottom:12,marginLeft:12}}>
                        <div style={{fontFamily:"'DM Serif Display',serif",fontSize:28,color:C.text}}>{Math.round(o.range_mi)} mi</div>
                        <div style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:"#78716c"}}>{(o.wh_per_km*1.609).toFixed(1)} Wh/mi</div>
                        {gainMi>0&&<div style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:C.green,marginTop:2}}>+{gainMi} mi vs {Math.round(road.base_speed*0.621)} mph</div>}
                      </div>
                      {ev.range>0&&(
                        <div style={{paddingBottom:12,marginLeft:8,borderLeft:"1px solid #fbbf24",paddingLeft:16}}>
                          <div style={{fontSize:9,color:"#92400e",letterSpacing:"0.08em",textTransform:"uppercase",marginBottom:4}}>EPA rated</div>
                          <div style={{fontFamily:"'Space Mono',monospace",fontSize:18,color:"#b45309"}}>{ev.range} mi</div>
                          <div style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:"#a8a29e"}}>{pctOfEpa}% of EPA</div>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Verdict + stats grid */}
                  <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(155px,1fr))",gap:12,marginBottom:18}}>
                    <div style={{gridColumn:"span 2",
                      background:canReach?"rgba(5,150,105,0.06)":optReach?"rgba(217,119,6,0.06)":"rgba(220,38,38,0.06)",
                      border:`1px solid ${canReach?"#6ee7b7":optReach?"#fcd34d":"#fca5a5"}`,
                      borderRadius:6,padding:"13px 15px"}}>
                      <div style={{fontSize:11,letterSpacing:"0.06em",fontWeight:700,marginBottom:6,
                        color:canReach?"#065f46":optReach?"#92400e":"#b91c1c"}}>
                        {canReach
                          ?`✓  You'll make it — arriving at ${arrivalSocPct}% SOC (${aboveReserve > 0 ? `+${aboveReserve}%` : `${aboveReserve}%`} above your ${resN}% reserve)`
                          :optReach
                          ?`⚠  At ${Math.round(road.base_speed*0.621)} mph you'd arrive at ${arrivalSocPct}% SOC — ${Math.abs(aboveReserve)}% below reserve. Drive ${Math.round(o.speed_kph*0.621)} mph to arrive at ${arrivalSocOptPct}% SOC instead`
                          :`✗  Charge stop needed — even at optimal ${Math.round(o.speed_kph*0.621)} mph, battery runs out ${Math.round((distKm-o.range_km)*0.621)} mi short`}
                      </div>
                      <div style={{fontSize:10,color:C.textMid,display:"flex",gap:10,flexWrap:"wrap"}}>
                        <span>Range @ {Math.round(road.base_speed*0.621)} mph: {Math.round(r.range_mi)} mi</span>
                        <span>·</span>
                        <span>Arrival SOC: {arrivalSocPct}%</span>
                        <span>·</span>
                        <span>Reserve: {resN}%</span>
                        <span>·</span>
                        <span>Available: {r.avail_kwh.toFixed(1)} kWh</span>
                        <span>·</span>
                        <span>SOH: {(r.soh*100).toFixed(0)}%</span>
                      </div>
                    </div>
                    {[
                      {label:"Range @ base speed",value:`${Math.round(r.range_mi)} mi`,sub:`${Math.round(r.range_km)} km`},
                      {label:"Arrival SOC",value:`${arrivalSocPct}%`,sub:`reserve ${resN}%`,accent: arrivalSocPct < resN},
                      {label:"Consumption",value:`${(r.wh_per_km*1.609).toFixed(1)} Wh/mi`},
                      {label:"Available energy",value:`${r.avail_kwh.toFixed(1)} kWh`},
                      {label:"Regen recovery",value:`${r.losses.regen_recovery.toFixed(1)}%`,sub:`${Math.round(r.regen_credit_w)} W`},
                      {label:"Temp derating",value:`${r.reduction_pct.toFixed(1)}%`,sub:"vs 25°C"},
                    ].map(s=><StatCard key={s.label} {...s}/>)}
                  </div>

                  {/* TABS */}
                  <div style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:8,
                    overflow:"hidden",boxShadow:"0 1px 4px rgba(0,0,0,0.06)"}}>
                    <div style={{display:"flex",borderBottom:`1px solid ${C.border}`,background:C.panelBg}}>
                      {[{id:"range",label:"Range vs Speed"},{id:"soc",label:"SOC Depletion"},
                        {id:"elev",label:"Elevation"},{id:"loss",label:"Loss Breakdown"}].map(t=>(
                        <button key={t.id} onClick={()=>setTab(t.id)}
                          style={{background:"none",border:"none",padding:"11px 16px",
                            fontFamily:"'Space Mono',monospace",fontSize:10,
                            letterSpacing:"0.1em",textTransform:"uppercase",cursor:"pointer",transition:"all 0.15s",
                            color:tab===t.id?C.amber:C.textMid,
                            borderBottom:tab===t.id?`2px solid ${C.amber}`:"2px solid transparent",
                            marginBottom:-1}}>
                          {t.label}
                        </button>
                      ))}
                    </div>

                    <div style={{padding:20}}>

                      {/* RANGE VS SPEED */}
                      {tab==="range"&&(
                        <div>
                          <div style={{fontSize:10,color:C.textMid,marginBottom:14,letterSpacing:"0.08em"}}>
                            Speed sweep · amber = optimal {Math.round(o.speed_kph*0.621)} mph
                            {ev.range>0&&` · green dashed = EPA rated ${ev.range} mi`}
                            {distKm>0&&`  · grey dashed = destination ${Math.round(distKm*0.621)} mi`}
                          </div>
                          <ResponsiveContainer width="100%" height={260}>
                            <LineChart data={sweep} margin={{top:5,right:24,left:0,bottom:5}}>
                              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                              <XAxis dataKey="speed" stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                label={{value:"mph",position:"insideBottomRight",offset:-4,style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                              <YAxis stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                label={{value:"mi",angle:-90,position:"insideLeft",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                              <Tooltip content={<ChartTip unit=" mi"/>}/>
                              {distKm>0&&<ReferenceLine y={Math.round(distKm*0.621)} stroke="#94a3b8" strokeDasharray="4 4"
                                label={{value:`${Math.round(distKm*0.621)} mi`,position:"right",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:"#94a3b8"}}}/>}
                              {ev.range>0&&<ReferenceLine y={ev.range} stroke={C.green} strokeDasharray="3 2"
                                label={{value:`EPA ${ev.range}mi`,position:"right",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.green}}}/>}
                              <ReferenceLine x={Math.round(o.speed_kph*0.621)} stroke={C.amberLight} strokeWidth={1.5}
                                label={{value:`★ ${Math.round(o.speed_kph*0.621)} mph`,position:"top",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.amberLight}}}/>
                              <Line type="monotone" dataKey="range" stroke={C.blue} strokeWidth={2.5} dot={false} name="Range"/>
                            </LineChart>
                          </ResponsiveContainer>
                          <div style={{fontSize:10,color:C.textMid,margin:"14px 0 8px",letterSpacing:"0.08em"}}>Consumption (Wh/mi) vs speed</div>
                          <ResponsiveContainer width="100%" height={130}>
                            <AreaChart data={sweep} margin={{top:0,right:24,left:0,bottom:5}}>
                              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                              <XAxis dataKey="speed" stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}/>
                              <YAxis stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}/>
                              <Tooltip content={<ChartTip unit=" Wh/mi"/>}/>
                              <ReferenceLine x={Math.round(o.speed_kph*0.621)} stroke={C.amberLight} strokeWidth={1.5}/>
                              <Area type="monotone" dataKey="wh_km" stroke={C.red} strokeWidth={1.5} fill="rgba(239,68,68,0.08)" name="Wh/mi"/>
                            </AreaChart>
                          </ResponsiveContainer>
                        </div>
                      )}

                      {/* SOC */}
                      {tab==="soc"&&(
                        <div>
                          <div style={{fontSize:10,color:C.textMid,marginBottom:14,letterSpacing:"0.08em"}}>
                            SOC% over distance · dashed red = reserve {resN}%
                            {distKm>0&&`  · dotted = destination ${Math.round(distKm*0.621)} mi`}
                          </div>
                          <ResponsiveContainer width="100%" height={300}>
                            <LineChart data={socD} margin={{top:5,right:24,left:0,bottom:5}}>
                              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                              <XAxis dataKey="dist" stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                label={{value:"mi",position:"insideBottomRight",offset:-4,style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                              <YAxis domain={[0,100]} stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                label={{value:"%",angle:-90,position:"insideLeft",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                              <Tooltip content={<ChartTip unit="%"/>}/>
                              <ReferenceLine y={resN} stroke={C.red} strokeDasharray="4 3"
                                label={{value:`${resN}% reserve`,position:"right",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.red}}}/>
                              {distKm>0&&<ReferenceLine x={Math.round(distKm*0.621)} stroke="#94a3b8" strokeDasharray="3 3"
                                label={{value:`${Math.round(distKm*0.621)}mi`,position:"top",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:"#94a3b8"}}}/>}
                              <Line type="monotone" dataKey={bL} stroke={C.blue} strokeWidth={2} dot={false} name={`Base ${Math.round(road.base_speed*0.621)} mph`}/>
                              <Line type="monotone" dataKey={oL} stroke="#34d399" strokeWidth={2} dot={false} name={`Optimal ${Math.round(o.speed_kph*0.621)} mph`}/>
                            </LineChart>
                          </ResponsiveContainer>
                        </div>
                      )}

                      {/* ELEVATION */}
                      {tab==="elev"&&(
                        <div>
                          {elev.flat
                            ?<div style={{textAlign:"center",padding:60,color:C.textMute,fontFamily:"'Space Mono',monospace",fontSize:11}}>
                              Elevation data unavailable — route assumed flat
                            </div>
                            :<>
                              <div style={{fontSize:10,color:C.textMid,marginBottom:14,letterSpacing:"0.08em"}}>
                                SRTM 90m · grade {elev.deg>=0?"+":""}{elev.deg.toFixed(2)}°
                                · ↑ {Math.round(elev.gain*3.281)} ft · ↓ {Math.round(elev.loss*3.281)} ft
                              </div>
                              <ResponsiveContainer width="100%" height={260}>
                                <AreaChart data={elevD} margin={{top:5,right:24,left:0,bottom:5}}>
                                  <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                                  <XAxis dataKey="dist" stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                    label={{value:"mi",position:"insideBottomRight",offset:-4,style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                                  <YAxis stroke="#cbd5e1" tick={{fontFamily:"'Space Mono',monospace",fontSize:10,fill:C.textMid}}
                                    label={{value:"ft",angle:-90,position:"insideLeft",style:{fontFamily:"'Space Mono',monospace",fontSize:9,fill:C.textMid}}}/>
                                  <Tooltip content={<ChartTip unit=" ft"/>}/>
                                  <Area type="monotone" dataKey="elev" stroke={C.amber} strokeWidth={2} fill="rgba(217,119,6,0.12)" name="Elevation"/>
                                </AreaChart>
                              </ResponsiveContainer>
                            </>}
                        </div>
                      )}

                      {/* LOSS BREAKDOWN */}
                      {tab==="loss"&&(
                        <div style={{maxWidth:480}}>
                          <div style={{fontSize:10,color:C.textMid,marginBottom:18,letterSpacing:"0.08em"}}>
                            % of gross power at {Math.round(road.base_speed*0.621)} mph
                          </div>
                          {[
                            {label:"Aerodynamic drag",key:"aero",color:C.blue},
                            {label:"Rolling resistance",key:"rolling",color:"#8b5cf6"},
                            {label:"Grade resistance",key:"grade",color:"#f97316"},
                            {label:"HVAC load",key:"hvac",color:"#06b6d4"},
                            {label:"Base aux (12V+)",key:"base_aux",color:C.textMid},
                            {label:"Thermal derating",key:"thermal",color:"#f43f5e"},
                          ].filter(l=>r.losses[l.key]>0.1).map(l=><LossBar key={l.key} {...l} pct={r.losses[l.key]}/>)}
                          <div style={{borderTop:`1px solid ${C.border}`,marginTop:10,paddingTop:10}}>
                            <LossBar label="Regen recovery (returned)" pct={r.losses.regen_recovery} color={C.green} isReturn/>
                          </div>
                          <div style={{marginTop:14,fontSize:10,color:C.textMid,lineHeight:1.7}}>
                            Net: {(r.wh_per_km*1.609).toFixed(1)} Wh/mi ({r.wh_per_km.toFixed(1)} Wh/km)
                            {ev.hwyE>0&&` · EPA hwy baseline: ${(ev.hwyE*10/16.0934*1.609).toFixed(1)} Wh/mi`}
                          </div>
                        </div>
                      )}

                    </div>
                  </div>
                </div>
              );
            })()}
          </div>
        </div>
      </div>
    </>
  );
}
