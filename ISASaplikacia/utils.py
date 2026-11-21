import json
import re
import datetime
import urllib.request
from typing import Any, Dict

from decimal import Decimal
from django.core.cache import cache
from django.conf import settings
from django.utils.html import format_html, escapejs, escape
from django.utils.safestring import mark_safe

from ISASaplikacia.models import GlobalneNastavenia 

def get_gemini_api_key():
    """
    🛡️ ISAS ENGINE: Bezpečné a vysoko výkonné získanie Gemini API kľúča.
    Priorita: 1. RAM Cache -> 2. Databáza (Klient) -> 3. .env súbor (Fallback/Vývoj)
    """
    cache_key = 'isas_gemini_api_key'
    try:
        api_key = cache.get(cache_key)
    except Exception:
        api_key = None
    
    # Ak je v cache prázdny string "", znamená to, že sme už predtým zistili, 
    # že kľúč neexistuje. Vrátime None, aby sme nezaťažovali DB.
    if api_key is not None:
        return api_key if api_key != "" else None

    db_read_ok = False
    # VÝKON: Používame .only(), aby sme z DB neťahali všetky texty a obrázky nastavení
    try:
        nastavenia = GlobalneNastavenia.objects.only('gemini_api_key').first()
        db_key = str(getattr(nastavenia, 'gemini_api_key', '') or '').strip() if nastavenia else ''
        if db_key:
            api_key = db_key
        else:
            raw_key = getattr(settings, 'GEMINI_API_KEY', None)
            api_key = str(raw_key).strip() if raw_key else None
        db_read_ok = True
    except Exception:
        raw_key = getattr(settings, 'GEMINI_API_KEY', None)
        api_key = str(raw_key).strip() if raw_key else None

    # Do cache ukladáme len pri úspešnom čítaní z DB, aby dočasný výpadok DB nezafixoval fallback na 1 hodinu.
    if db_read_ok:
        try:
            cache.set(cache_key, api_key if api_key else "", timeout=3600)
        except Exception:
            pass
    
    return api_key

GEMINI_PRICING_CATALOG: Dict[str, Dict[str, Any]] = {
    # 🚀 GEMINI 4 ŠPIČKOVÝ MODEL (Prechod zľavy: 01.12.2026)
    'gemini-4-carbon': {
        'cutoff': datetime.date(2026, 12, 1),
        'before': {'in': Decimal('2.00'), 'out': Decimal('10.00'), 'cached': Decimal('0.10')},
        'after':  {'in': Decimal('4.00'), 'out': Decimal('20.00'), 'cached': Decimal('0.20')},
    },

    # 🧠 GEMINI 4 NEON (Frontier, iba implicitné cachovanie)
    'gemini-4-neon': {
        'rate': {'in': Decimal('3.00'), 'out': Decimal('15.00'), 'cached': Decimal('0.15')},
    },

    # ⚡ ŠTANDARDNÝ MODEL GEMINI 4 HYDROGEN (Prechod zľavy: 01.01.2027)
    'gemini-4-hydrogen': {
        'cutoff': datetime.date(2027, 1, 1),
        'before': {'in': Decimal('0.75'), 'out': Decimal('3.75'), 'cached': Decimal('0.075')},
        'after':  {'in': Decimal('1.50'), 'out': Decimal('7.50'), 'cached': Decimal('0.150')},
    },
}

def vypocitaj_ai_cenu_usd(in_tokens: Any = 0, out_tokens: Any = 0, cached_tokens: Any = 0, model_name: str | None = None) -> Decimal:
    """
    💰 ISAS CORE: Defenzívny výpočet ceny za AI tokeny (USD).
    - Model sa riadi výhradne z .env cez settings.GEMINI_MODEL (default: gemini-4-hydrogen).
    - Podporuje výhradne: gemini-4-hydrogen, gemini-4-neon, gemini-4-carbon.
    - Ceny automaticky zohľadňujú dátum zlomu uvádzacích zliav.
    """
    try:
        in_t = Decimal(str(in_tokens)) if in_tokens not in [None, ""] else Decimal('0.00')
        out_t = Decimal(str(out_tokens)) if out_tokens not in [None, ""] else Decimal('0.00')
        cached_t = Decimal(str(cached_tokens)) if cached_tokens not in [None, ""] else Decimal('0.00')
    except (ValueError, TypeError, Exception):
        in_t, out_t, cached_t = Decimal('0.00'), Decimal('0.00'), Decimal('0.00')

    if not (in_t.is_finite() and out_t.is_finite() and cached_t.is_finite()):
        in_t, out_t, cached_t = Decimal('0.00'), Decimal('0.00'), Decimal('0.00')

    in_t = max(Decimal('0.00'), in_t)
    out_t = max(Decimal('0.00'), out_t)
    cached_t = max(Decimal('0.00'), cached_t)

    # Matematická ochrana: Cachované tokeny nemôžu prevyšovať celkový vstup
    cached_t = min(cached_t, in_t)
    non_cached_in = max(Decimal('0.00'), in_t - cached_t)

    # 1. Zistenie cieľového modelu
    target_model = str(model_name or getattr(settings, 'GEMINI_MODEL', 'gemini-4-hydrogen')).lower().strip().removeprefix("models/")
    config: Dict[str, Any] = GEMINI_PRICING_CATALOG.get(target_model, GEMINI_PRICING_CATALOG['gemini-4-hydrogen'])

    # 2. Určenie presných sadzieb podľa dátumu
    dnes = datetime.date.today()
    if config.get('cutoff'):
        sadzby = config['after'] if dnes >= config['cutoff'] else config['before']
    else:
        sadzby = config['rate']

    # 3. Finálny výpočet
    milion = Decimal('1000000.00')
    in_cena = (non_cached_in / milion) * sadzby['in']
    cached_cena = (cached_t / milion) * sadzby['cached']
    out_cena = (out_t / milion) * sadzby['out']

    return (in_cena + cached_cena + out_cena).quantize(Decimal('0.000001'))

def get_google_maps_api_key():
    """
    🛡️ ISAS ENGINE: Bezpečné a výkonné získanie Google Maps API kľúča.
    Priorita: 1. RAM Cache -> 2. Databáza (Klient) -> 3. .env súbor (Fallback/Vývoj)
    """
    cache_key = 'isas_google_maps_api_key'
    try:
        api_key = cache.get(cache_key)
    except Exception:
        api_key = None
    
    # Ak je v cache prázdny string "", znamená to, že sme už predtým zistili, 
    # že kľúč neexistuje. Vrátime None, aby sme nezaťažovali DB.
    if api_key is not None:
        return api_key if api_key != "" else None

    db_read_ok = False
    # VÝKON: Používame .only(), aby sme z DB neťahali všetky texty a obrázky nastavení
    try:
        nastavenia = GlobalneNastavenia.objects.only('google_maps_api_key').first()
        db_key = str(getattr(nastavenia, 'google_maps_api_key', '') or '').strip() if nastavenia else ''
        if db_key:
            api_key = db_key
        else:
            raw_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', None)
            api_key = str(raw_key).strip() if raw_key else None
        db_read_ok = True
    except Exception:
        raw_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', None)
        api_key = str(raw_key).strip() if raw_key else None

    # Do cache ukladáme len pri úspešnom čítaní z DB, aby dočasný výpadok DB nezafixoval fallback na 1 hodinu.
    if db_read_ok:
        try:
            cache.set(cache_key, api_key if api_key else "", timeout=3600)
        except Exception:
            pass
    
    return api_key

def get_kurz_k_usd(cielova_mena: str | None = None):
    """
    🛡️ ISAS ZERO-DEBT CURRENCY ENGINE:
    Získa živý kurz USD voči cieľovej mene z open.er-api.com s dvojúrovňovou Redis RAM cache.
    Nula hardkódu, nula statických tabuliek. Ak kurz nie je dostupný, vracia None.
    """
    if not cielova_mena:
        try:
            from Evidencia.utils_helper import get_firemna_mena
            cielova_mena = get_firemna_mena()
        except Exception:
            cielova_mena = None

    cielova_mena_clean = str(cielova_mena or '').strip().upper()
    if not cielova_mena_clean:
        return None

    if cielova_mena_clean == 'USD':
        return Decimal('1.00')

    cache_key = 'isas_live_kurzy_usd_matrix'
    last_known_key = 'isas_last_known_kurzy_usd_matrix'

    kurzy = None
    try:
        kurzy = cache.get(cache_key)
    except Exception:
        kurzy = None

    if kurzy is None or not isinstance(kurzy, dict):
        try:
            req = urllib.request.Request(
                'https://open.er-api.com/v6/latest/USD',
                headers={'User-Agent': 'ISAS-Core-SaaS/1.0'}
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    if data.get('result') == 'success' and 'rates' in data:
                        kurzy = data['rates']
                        try:
                            # 1. Bežná 24-hodinová cache
                            cache.set(cache_key, kurzy, 86400)
                            # 2. Trvalá "Last Known Good" cache pre prípad sieťového výpadku
                            cache.set(last_known_key, kurzy, None)
                        except Exception:
                            pass
        except Exception:
            kurzy = None

        # 🛡️ ANTI-STAMPEDE POISTKA: Ak live dotaz zlyhal, použijeme reálnu zálohu a zamkneme ďalšie pokusy na 15 minút
        if not kurzy or not isinstance(kurzy, dict):
            try:
                kurzy = cache.get(last_known_key)
                if kurzy and isinstance(kurzy, dict):
                    # Predídeme 3s sieťovému zdržaniu na ďalších requestoch
                    cache.set(cache_key, kurzy, 900)
                else:
                    # Ak je server úplne nový bez dát a bez netu, nastavíme prázdny marker na 5 minút
                    cache.set(cache_key, {}, 300)
            except Exception:
                kurzy = None

    if kurzy and isinstance(kurzy, dict) and cielova_mena_clean in kurzy:
        try:
            kurz = Decimal(str(kurzy[cielova_mena_clean]))
            if kurz > Decimal('0.00') and kurz.is_finite():
                return kurz
        except Exception:
            pass

    return None

def get_isas_magia(
    typ_zobrazenia='tabulka',
    search_placeholder="🔍 Vyhľadávanie...",
    live_search_endpoint=None,
    live_search_ikona="🛡️",
    live_search_kategoria="ZÁZNAM",
    upozornenie_prazdny_vyber="Najskôr zaškrtnite aspoň jeden záznam zo zoznamu.",
    vyzaduje_potvrdenie=False,
    potvrdenie_nadpis="Ste si istý? 🚨",
    potvrdenie_text="Tento krok natrvalo odstráni dáta. 💥",
    skryt_vyhladavanie=False,
    skryt_akcie=False,
    custom_buttons_html="",
    custom_js="",
    custom_css="",
    ignore_default_action_ids=None,
    cisty_dashboard=False,
    bez_tlacidla_odstranit=False,
    is_user_admin=False,
    selector_texts=None,
    katalogovy_dashboard=False,
    katalog_choices=None
):
    if ignore_default_action_ids is None:
        ignore_default_action_ids = []

    cat_data = []
    if katalogovy_dashboard and katalog_choices:
        color_themes = [
            {'bg': 'linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%)', 'text': '#1e40af', 'shadow': 'rgba(59, 130, 246, 0.2)'},
            {'bg': 'linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%)', 'text': '#065f46', 'shadow': 'rgba(16, 185, 129, 0.2)'},
            {'bg': 'linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%)', 'text': '#991b1b', 'shadow': 'rgba(239, 68, 68, 0.2)'},
            {'bg': 'linear-gradient(135deg, #faf5ff 0%, #f3e8ff 100%)', 'text': '#581c87', 'shadow': 'rgba(168, 85, 247, 0.2)'},
            {'bg': 'linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%)', 'text': '#9a3412', 'shadow': 'rgba(249, 115, 22, 0.2)'},
        ]
        for idx, item in enumerate(katalog_choices):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            key, full_label = item[0], item[1]
            label_str = str(full_label or '').strip()
            match = re.match(r'^([^\w]+)\s*(.*)$', label_str, re.UNICODE)
            if match:
                ikona = match.group(1).strip()
                cisty_nazov = match.group(2).strip()
            else:
                ikona = '📦'
                cisty_nazov = label_str
            theme = color_themes[idx % len(color_themes)]
            cat_data.append({'key': key, 'label': cisty_nazov, 'icon': ikona, **theme})

    config = {
        'typ_zobrazenia': typ_zobrazenia,
        'search_placeholder': search_placeholder,
        'live_search_endpoint': live_search_endpoint,
        'live_search_ikona': live_search_ikona,
        'live_search_kategoria': live_search_kategoria,
        'upozornenie_prazdny_vyber': upozornenie_prazdny_vyber,
        'vyzaduje_potvrdenie': vyzaduje_potvrdenie,
        'potvrdenie_nadpis': potvrdenie_nadpis,
        'potvrdenie_text': potvrdenie_text,
        'skryt_vyhladavanie': skryt_vyhladavanie,
        'skryt_akcie': skryt_akcie,
        'custom_buttons_html': custom_buttons_html,
        'ignore_default_action_ids': ignore_default_action_ids,
        'cisty_dashboard': cisty_dashboard,
        'bez_tlacidla_odstranit': bez_tlacidla_odstranit,
        'is_user_admin': is_user_admin,
        'selector_texts': selector_texts or {},
        'katalogovy_dashboard': katalogovy_dashboard,
        'katalog_choices': cat_data
    }

    safe_json_config = json.dumps(config, default=str).replace('</', '\\u003c/')
    html = f'<script id="isas-magia-config" type="application/json">{safe_json_config}</script>'
    if custom_css:
        html += f'<style>{custom_css}</style>'
    if custom_js:
        html += f'<script>document.addEventListener("DOMContentLoaded", function() {{ {custom_js} }});</script>'

    return mark_safe(html)

# =========================================================================
# 🎨 ISAS UI COMPONENT FACTORY (DRY Architektúra pre celý systém)
# =========================================================================

# --- 1. ZÁKLADNÉ IDENTIFIKAČNÉ KARTY ---

def render_isas_avatar(icon, title, subtitle, bg_gradient, shadow_color, extra_html="", image_url=None):
    """Generuje štandardizovanú vertikálnu kartu identity (Avatar) pre bežné modely."""
    if image_url:
        avatar_div = format_html('<img src="{}" alt="Avatar" width="34" height="34" style="background: white; border-radius: 10px; object-fit: contain; box-shadow: 0 2px 4px rgba(0,0,0,0.05); border: 1px solid rgba(0,0,0,0.05); margin: 0 auto;" onerror="this.style.display=\'none\';"/>', image_url)
    else:
        avatar_div = format_html('<div style="width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, {}); color: white; font-size: 16px; border-radius: 10px; flex-shrink: 0; box-shadow: 0 3px 8px {}; margin: 0 auto;">{}</div>', mark_safe(bg_gradient), mark_safe(shadow_color), mark_safe(icon))
        
    subtitle_html = format_html('<span style="font-size: 10px; font-weight: 600; color: #64748b; margin-top: 1px;">{}</span>', subtitle) if subtitle else ""
    
    return format_html(
        '<div style="display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; width: 100%;">'
        '{}'
        '<span style="font-size: 13px; font-weight: 900; color: #0f172a; margin-top: 4px; letter-spacing: 0.3px; line-height: 1.2; word-break: break-word; max-width: 100%;">{}</span>'
        '{}'
        '<div style="margin-top: 4px;">{}</div>'
        '</div>',
        avatar_div, title, mark_safe(subtitle_html), mark_safe(extra_html)
    )

def render_isas_header_card(icon, title, content_html):
    """Generuje veľkú horizontálnu hlavičku (používané v Singleton modeloch)."""
    return format_html(
        '<div style="display: flex; align-items: center; gap: 20px;">'
        '<div style="width: 70px; height: 70px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #1e293b, #0f172a); color: white; font-size: 30px; border-radius: 18px; box-shadow: 0 10px 20px rgba(15,23,42,0.2); flex-shrink: 0;">{}</div>'
        '<div style="display: flex; flex-direction: column; gap: 6px; min-width: 0;">'
        '<div style="font-size: 22px; font-weight: 900; color: #0f172a; letter-spacing: -0.5px; text-transform: uppercase; word-break: break-word; line-height: 1.2;">{}</div>'
        '<div style="display: flex; gap: 8px; flex-wrap: wrap;">{}</div>'
        '</div></div>',
        mark_safe(icon), title, mark_safe(content_html)
    )

# --- 2. ODZNAKY A AKČNÉ PRVKY ---

def render_isas_dashboard_action_btn(url, icon, title, subtitle, icon_bg_gradient="#3b82f6, #1d4ed8", icon_shadow="rgba(59, 130, 246, 0.4)"):
    """Generuje veľké akčné tlačidlo pre dashboardy (ikona vľavo, texty v strede, šípka vpravo)."""
    return format_html(
        '<a href="{}" style="display: inline-flex; align-items: center; gap: 16px; background: linear-gradient(135deg, #ffffff, #f8fafc); border: 1px solid #cbd5e1; border-radius: 16px; padding: 12px 20px; text-decoration: none; box-shadow: 0 10px 25px rgba(0,0,0,0.05); transition: all 0.3s ease; width: 100%; max-width: 400px; box-sizing: border-box;" onmouseover="this.style.transform=\'translateY(-3px)\'; this.style.boxShadow=\'0 15px 35px rgba(59, 130, 246, 0.15)\'; this.style.borderColor=\'#3b82f6\';" onmouseout="this.style.transform=\'translateY(0)\'; this.style.boxShadow=\'0 10px 25px rgba(0,0,0,0.05)\'; this.style.borderColor=\'#cbd5e1\';">'
            '<div style="background: linear-gradient(135deg, {}); color: white; width: 48px; height: 48px; display: flex; align-items: center; justify-content: center; border-radius: 12px; font-size: 22px; box-shadow: 0 4px 10px {}; flex-shrink: 0;">{}</div>'
            '<div style="display: flex; flex-direction: column; flex-grow: 1; overflow: hidden;">'
                '<span style="color: #0f172a; font-size: 15px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{}</span>'
                '<span style="color: #64748b; font-size: 11px; font-weight: 600; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{}</span>'
            '</div>'
            '<div style="color: #3b82f6; font-size: 20px; font-weight: bold; flex-shrink: 0; margin-left: 8px;">➔</div>'
        '</a>',
        url or "#", mark_safe(icon_bg_gradient), mark_safe(icon_shadow), mark_safe(icon), title, subtitle
    )

def render_isas_badge(text, icon="", type_color="neutral", wrapper=True, prefix_text=None, suffix_html=None, monospace=False, copy_value=None, custom_bg=None, custom_text=None, custom_border=None, custom_shadow=None, css_class=None, tooltip=None, custom_style=None, data_attrs=None, html_id=None):
    colors = {
        'success': ('#dcfce7', '#166534', '#bbf7d0', 'rgba(22, 101, 52, 0.1)', '#15803d'),
        'danger': ('#fef2f2', '#991b1b', '#fecaca', 'rgba(153, 27, 27, 0.1)', '#b91c1c'),
        'warning': ('#fffbeb', '#b45309', '#fde68a', 'rgba(180, 83, 9, 0.1)', '#d97706'),
        'info': ('#eff6ff', '#1e40af', '#bfdbfe', 'rgba(30, 64, 175, 0.1)', '#1d4ed8'),
        'neutral': ('#f8fafc', '#0f172a', '#e2e8f0', 'rgba(0, 0, 0, 0.02)', '#94a3b8'),
        'purple': ('#faf5ff', '#581c87', '#e9d5ff', 'rgba(107, 33, 168, 0.1)', '#a855f7'),
    }
    bg, text_c, border, shadow, prefix_c = colors.get(type_color, colors['neutral'])
    
    bg = custom_bg or bg
    text_c = custom_text or text_c
    border = custom_border or border
    shadow = custom_shadow or shadow
    
    icon_html = format_html('<span style="font-size: 11px; margin-right: 4px;">{}</span>', mark_safe(icon)) if icon else ""
    prefix_html = format_html('<span style="color: {}; margin-right: 4px; font-weight: 800;">{}</span>', prefix_c, prefix_text) if prefix_text else ""
    suffix_html_safe = format_html('<span style="margin-left: 4px;">{}</span>', mark_safe(suffix_html)) if suffix_html else ""
    font_family = "monospace" if monospace else "inherit"
    
    js_attr = ""
    cursor_style = ""
    hover_style = ""
    if copy_value:
        safe_js = escapejs(copy_value)
        js_code = f"navigator.clipboard.writeText('{safe_js}'); var el=this; var old=el.innerHTML; el.innerHTML='✅ Skopírované'; el.style.background='#dcfce7'; el.style.borderColor='#86efac'; el.style.color='#166534'; setTimeout(function(){{ el.innerHTML=old; el.style.background='{bg}'; el.style.borderColor='{border}'; el.style.color='{text_c}'; }}, 1000);"
        js_attr = format_html(' onclick="{}"', mark_safe(js_code))
        cursor_style = "cursor: pointer;"
        hover_style = mark_safe(' onmouseover="this.style.transform=\'scale(1.05)\';" onmouseout="this.style.transform=\'scale(1)\';"')
    
    tooltip_attr = format_html(' title="{}"', tooltip) if tooltip else ""
    if tooltip and not copy_value:
        cursor_style = "cursor: help;"
        
    id_attr = format_html(' id="{}"', html_id) if html_id else ""
    class_attr = format_html(' class="{}"', css_class) if css_class else ""
    extra_style = f" {custom_style}" if custom_style else ""
    
    data_html = ""
    if isinstance(data_attrs, dict):
        data_html = mark_safe(" ".join([f'data-{escape(k)}="{escape(v)}"' for k, v in data_attrs.items()]))
    
    safe_text = "" if text is None else text
    badge = format_html(
        '<span{}{}{}{}{} style="background: {}; color: {}; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 800; border: 1px solid {}; box-shadow: 0 2px 5px {}; display: inline-flex; align-items: center; white-space: nowrap; transition: all 0.2s; {}{}{}">'
        '{}{}<span style="font-family: {};">{}</span>{}'
        '</span>',
        id_attr, class_attr, js_attr, tooltip_attr, format_html(' {}', data_html) if data_html else "", bg, text_c, border, shadow, cursor_style, extra_style, hover_style,
        icon_html, prefix_html, font_family, safe_text, suffix_html_safe
    )
    return format_html('<div style="display: flex; justify-content: center; width: 100%; margin-bottom: 4px;">{}</div>', badge) if wrapper else badge

def render_isas_link_btn(url, text, icon="", type_color="info", wrapper=False, counter_val=None, full_width=False, square=False, is_button=False, data_attrs=None, onclick=None, suffix_html=None, layout="row", css_class=None, custom_style=None, custom_hover_style=None, tooltip=None, target=None):
    colors = {
        'info': ('#eff6ff', '#1d4ed8', '#bfdbfe', '#93c5fd'),
        'success': ('#f0fdf4', '#15803d', '#bbf7d0', '#86efac'),
        'warning': ('#fffbeb', '#b45309', '#fde68a', '#fcd34d'),
        'danger': ('#fef2f2', '#991b1b', '#fecaca', '#fca5a5'),
        'neutral': ('#ffffff', '#0f172a', '#e2e8f0', '#94a3b8'),
        'dashed': ('#f8fafc', '#64748b', '#cbd5e1', '#94a3b8'),
        'dark': ('#1e293b', '#ffffff', '#0f172a', '#000000'),
        'purple': ('#faf5ff', '#7e22ce', '#e9d5ff', '#d8b4fe'),
    }
    bg, text_c, border, hover_border = colors.get(type_color, colors['info'])
    
    border_style = "1px dashed" if type_color == "dashed" else "1px solid"
    width_style = "width: 100%; max-width: 200px; justify-content: space-between;" if full_width else "justify-content: center;"
    padding_style = "width: 32px; height: 32px; padding: 0;" if square else "padding: 8px 16px;"
    
    flex_dir = "column" if layout == "col" else "row"
    icon_margin = "margin-bottom: 4px;" if layout == "col" else f"margin-right: {'0' if square else '8'}px;"
    
    text_html = "" if square else format_html('{}', text)
    icon_html = format_html('<span style="font-size: 13px; {}">{}</span>', icon_margin, mark_safe(icon)) if icon else ""
    
    if suffix_html and layout == "col":
        suffix_safe = format_html('<div style="margin-top: 4px; opacity: 0.9;">{}</div>', mark_safe(suffix_html))
    elif suffix_html:
        suffix_safe = format_html('<span style="margin-left: 6px; opacity: 0.7;">{}</span>', mark_safe(suffix_html))
    else:
        suffix_safe = ""
        
    counter_html = ""
    if counter_val is not None:
        counter_html = format_html('<span style="background: {}; color: {}; padding: 2px 6px; border-radius: 4px; font-size: 10px; border: 1px solid {}; margin-left: 8px;">{}</span>', bg, text_c, border, counter_val)
        
    data_html = ""
    if isinstance(data_attrs, dict):
        data_html = mark_safe(" ".join([f'data-{escape(k)}="{escape(v)}"' for k, v in data_attrs.items()]))
        
    onclick_html = format_html(' onclick="{}"', mark_safe(onclick)) if onclick else ""
    class_attr = format_html(' class="{}"', css_class) if css_class else ""
    tooltip_attr = format_html(' title="{}"', tooltip) if tooltip else ""
    
    extra_style = f" {custom_style}" if custom_style else ""
    extra_hover = f" {custom_hover_style}" if custom_hover_style else ""
    
    common_style = f"display: inline-flex; flex-direction: {flex_dir}; align-items: center; justify-content: center; text-align: center; white-space: nowrap; background: {bg}; color: {text_c}; {padding_style}; border-radius: 8px; font-weight: 800; text-decoration: none; border: {border_style} {border}; font-size: 12px; text-transform: uppercase; box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: all 0.2s; cursor: pointer; {width_style}{extra_style}"
    hover_js = f" onmouseover=\"this.style.borderColor='{hover_border}'; this.style.transform='translateY(-1px)'; this.style.boxShadow='0 4px 6px rgba(0,0,0,0.1)';{extra_hover}\" onmouseout=\"this.style.borderColor='{border}'; this.style.transform='translateY(0)'; this.style.boxShadow='0 2px 4px rgba(0,0,0,0.05)';\""

    if is_button:
        btn = format_html(
            '<button type="button" style="{}" {}{}{} {} {}>'
            '{}{}{}{}'
            '</button>',
            common_style, class_attr, tooltip_attr, mark_safe(hover_js), data_html, onclick_html, icon_html, text_html, mark_safe(counter_html), mark_safe(suffix_safe)
        )
    else:
        if target is None:
            clean_url = str(url).split('?')[0].split('#')[0].lower() if url else ""
            is_pdf = clean_url.endswith('.pdf')
            target = "_blank" if url and (str(url).startswith(('http://', 'https://')) or is_pdf) else "_self"
        
        rel_attr = mark_safe(' rel="noopener noreferrer"') if target == "_blank" else ""

        btn = format_html(
            '<a href="{}" target="{}"{} style="{}" {}{}{} {} {}>'
            '{}{}{}{}'
            '</a>',
            url or "#", target, rel_attr, common_style, class_attr, tooltip_attr, mark_safe(hover_js), data_html, onclick_html, icon_html, text_html, mark_safe(counter_html), mark_safe(suffix_safe)
        )
        
    return format_html('<div style="display: flex; justify-content: center; width: 100%;">{}</div>', btn) if wrapper else btn

# --- 3. KONTAJNERY A LAYOUT ENGINE ---

def render_isas_entity_card(icon, title, metadata_list=None):
    """Generuje horizontálnu kartu entity (vhodné pre zoznamy s metadátami)."""
    meta_html = ""
    if metadata_list:
        meta_items = [format_html('<div style="font-size: 10px; color: #64748b; margin-top: 2px;">{}</div>', mark_safe(m)) for m in metadata_list]
        meta_html = mark_safe(''.join(meta_items))
        
    return format_html(
        '<div style="display: flex; justify-content: center; align-items: center; width: 100%; height: 100%;">'
        '<div style="background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%); border: 1px solid #cbd5e1; padding: 8px 16px; border-radius: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.04); display: inline-flex; align-items: center; gap: 10px; min-width: 220px; max-width: 280px; transition: all 0.3s ease;">'
        '<span style="font-size: 16px; filter: drop-shadow(0 2px 2px rgba(0,0,0,0.1)); flex-shrink: 0;">{}</span>'
        '<div style="display: flex; flex-direction: column; text-align: left; overflow: hidden;">'
        '<span style="font-size: 13px; font-weight: 900; color: #1e293b; line-height: 1.4; word-wrap: break-word;">{}</span>'
        '{}'
        '</div>'
        '</div></div>',
        mark_safe(icon), title, mark_safe(meta_html)
    )

def render_isas_singleton_card(title, icon, title_color, rows_html):
    """Generuje širokú informačnú kartu špecifickú pre Singleton modely."""
    return format_html(
        '<div style="background: white; border: 1px solid #e2e8f0; padding: 16px; border-radius: 16px; height: 100%; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); display: flex; flex-direction: column; gap: 12px;">'
        '<div style="display: flex; align-items: center; gap: 6px; font-size: 11px; color: {}; font-weight: 800; text-transform: uppercase; border-bottom: 1px solid #f1f5f9; padding-bottom: 8px;">{} {}</div>'
        '<div style="display: flex; flex-direction: column; gap: 8px;">{}</div>'
        '</div>',
        title_color, mark_safe(icon), title, mark_safe(rows_html)
    )

def render_isas_singleton_row(content_html, is_dashed=True, border_color="#e2e8f0", padding_top=8):
    """Generuje riadok vo vnútri Singleton karty s voliteľným oddelovačom."""
    if is_dashed:
        border = f'border-top: 1px dashed {border_color}; padding-top: {padding_top}px; width: 100%;'
    elif border_color:
        border = f'border-top: 1px solid {border_color}; padding-top: {padding_top}px; width: 100%;'
    else:
        border = ''
    return format_html('<div style="{}">{}</div>', mark_safe(border), mark_safe(content_html))

def render_isas_layout(content_html, layout="col", gap=4, center=False, columns=2, bg_color=None, border_color=None, padding=None, border_radius=None, min_width=None, max_width=None, shadow=None, custom_style=None, html_id=None, css_class=None, data_attrs=None):
    """Univerzálny layout engine (nahrádza samostatné grid, stack a flex funkcie)."""
    if layout == "grid":
        base_style = f'display: grid; grid-template-columns: repeat({columns}, 1fr); gap: {gap}px; width: 100%;'
    else:
        align = "center" if center else "flex-start"
        direction = "column" if layout == "col" else "row"
        justify = "center" if center and layout == "row" else "flex-start"
        base_style = f'display: flex; flex-direction: {direction}; align-items: {align}; justify-content: {justify}; gap: {gap}px; width: 100%;'
    
    if bg_color: base_style += f' background: {bg_color};'
    if border_color: base_style += f' border: 1px solid {border_color};'
    if padding: base_style += f' padding: {padding};'
    if border_radius: base_style += f' border-radius: {border_radius};'
    if min_width: base_style += f' min-width: {min_width};'
    if max_width: base_style += f' max-width: {max_width};'
    if shadow: base_style += f' box-shadow: {shadow};'
    if custom_style: base_style += f' {custom_style}'
    
    id_attr = format_html(' id="{}"', html_id) if html_id else ""
    class_attr = format_html(' class="{}"', css_class) if css_class else ""
    
    data_html = ""
    if isinstance(data_attrs, dict):
        data_html = mark_safe(" ".join([f'data-{escape(k)}="{escape(v)}"' for k, v in data_attrs.items()]))
    data_attr_str = format_html(' {}', data_html) if data_html else ""
    
    return format_html('<div{}{}{} style="{}">{}</div>', id_attr, class_attr, data_attr_str, mark_safe(base_style), mark_safe(content_html))

def render_isas_flex_between(left_content, right_content, extra_style=""):
    """Generuje riadok s prvkami zarovnanými na protiľahlé strany (Left ... Right)."""
    return format_html(
        '<div style="display: flex; justify-content: space-between; align-items: center; width: 100%; {}">'
        '<span>{}</span><span>{}</span>'
        '</div>',
        mark_safe(extra_style), mark_safe(left_content), mark_safe(right_content)
    )

# --- 4. DÁTOVÉ BLOKY A ŠTATISTIKY ---

def render_isas_input_number(name, value, min_val=1, suffix="", width=50):
    """Generuje interaktívne číselné vstupné pole (napr. pre úpravu množstva v košíku)."""
    style = (
        f"width: {width}px; padding: 8px 6px; font-weight: 800; font-size: 14px; color: #0f172a; text-align: center; "
        "border: 2px solid #e2e8f0; border-radius: 10px; background: white; "
        "box-shadow: 0 2px 4px rgba(0,0,0,0.03); outline: none; transition: all 0.2s ease-in-out; box-sizing: border-box;"
    )
    focus_js = "this.style.borderColor='#6366f1'; this.style.boxShadow='0 0 0 4px rgba(99, 102, 241, 0.15), 0 4px 6px rgba(0,0,0,0.05)';"
    blur_js = "this.style.borderColor='#e2e8f0'; this.style.boxShadow='0 2px 4px rgba(0,0,0,0.03)';"
    
    suffix_html = format_html('<span style="font-weight: 900; color: #94a3b8; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; margin-left: 8px;">{}</span>', suffix) if suffix else ""
    
    safe_value = "" if value is None else value
    safe_min = 1 if min_val is None else min_val
    return format_html(
        '<div style="display: flex; align-items: center; justify-content: flex-start; flex-wrap: nowrap; white-space: nowrap;">'
        '<input type="number" name="{}" value="{}" min="{}" style="{}" onfocus="{}" onblur="{}">'
        '{}'
        '</div>',
        name, safe_value, safe_min, style, mark_safe(focus_js), mark_safe(blur_js), mark_safe(suffix_html)
    )

def render_isas_key_value(label, value, subtext=None, icon=None):
    """Generuje štandardizovaný vertikálny blok Kľúč: Hodnota."""
    icon_html = format_html('<span style="margin-right: 4px;">{}</span>', mark_safe(icon)) if icon else ""
    subtext_html = format_html('<div style="font-size: 11px; color: #64748b; margin-top: 4px;">{}</div>', subtext) if subtext else ""
    safe_val = "—" if value is None or str(value).strip() == "" else value
    return format_html(
        '<div style="font-size: 10px; color: #64748b; font-weight: 800; text-transform: uppercase; margin-bottom: 2px;">{}{}</div>'
        '<div style="color: #334155; font-size: 12px; font-weight: 600; word-break: break-word;">{}</div>'
        '{}',
        icon_html, label or "", safe_val, mark_safe(subtext_html)
    )

def render_isas_premium_card(icon, title, main_text, sub_left, sub_right, grad_start, grad_end, shadow_color):
    """Generuje prémiovú kartu s prechodom (napr. pre Bankový účet)."""
    return format_html(
        '<div style="background: linear-gradient(135deg, {}, {}); color: white; padding: 12px; border-radius: 12px; box-shadow: 0 4px 10px {}; position: relative; overflow: hidden;">'
        '<div style="position: absolute; right: -10px; top: -10px; font-size: 50px; opacity: 0.1;">{}</div>'
        '<div style="font-size: 10px; color: rgba(255,255,255,0.8); font-weight: 800; text-transform: uppercase; margin-bottom: 4px; position: relative; z-index: 2;">{}</div>'
        '<div style="font-family: monospace; font-size: 13px; font-weight: 800; letter-spacing: 0.5px; margin-bottom: 4px; position: relative; z-index: 2;">{}</div>'
        '<div style="display: flex; justify-content: space-between; font-size: 10px; color: rgba(255,255,255,0.9); font-weight: 700; position: relative; z-index: 2;"><span>{}</span><span>{}</span></div>'
        '</div>',
        grad_start, grad_end, shadow_color, mark_safe(icon), title, main_text, sub_left, sub_right
    )

def render_isas_mini_stat_card(top_label, top_val, bot_label, bot_val, third_label=None, third_val=None, third_val_color="#0f172a", top_val_color="#0f172a", bot_val_color="#0f172a", css_class=None, data_attrs=None):
    third_html = ""
    if third_label is not None and third_val is not None:
        third_html = format_html(
            '<div style="font-size: 9px; color: #64748b; font-weight: 800; border-top: 1px solid #f1f5f9; padding-top: 4px; margin-top: 4px; width: 100%; text-align: center; text-transform: uppercase;">{}</div><div style="font-size: 12px; font-weight: 900; color: {};">{}</div>',
            third_label, third_val_color, third_val
        )
        
    class_attr = format_html(' class="{}"', css_class) if css_class else ""
    data_html = ""
    if isinstance(data_attrs, dict):
        data_html = mark_safe(" ".join([f'data-{escape(k)}="{escape(v)}"' for k, v in data_attrs.items()]))
        
    return format_html(
        '<div{}{} style="display: flex; flex-direction: column; align-items: center; background: #ffffff; padding: 6px 12px; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 2px 6px rgba(0,0,0,0.03); min-width: 140px; max-width: 100%; margin: 0 auto; text-align: center;">'
        '<div style="font-size: 9px; color: #64748b; font-weight: 800; text-transform: uppercase;">{}</div><div style="font-size: 12px; font-weight: 900; color: {}; margin-bottom: 4px; word-break: break-word;">{}</div>'
        '<div style="font-size: 9px; color: #64748b; font-weight: 800; border-top: 1px solid #f1f5f9; padding-top: 4px; width: 100%; text-align: center; text-transform: uppercase;">{}</div><div style="font-size: 12px; font-weight: 900; color: {}; word-break: break-word;">{}</div>'
        '{}'
        '</div>',
        class_attr, format_html(' {}', data_html) if data_html else "", top_label, top_val_color, top_val, bot_label, bot_val_color, bot_val, mark_safe(third_html)
    )

def render_isas_time_card(primary_label, primary_val, secondary_label, secondary_val, badge_label=None, badge_val=None):
    """Generuje štandardizovanú časovú os (Time Card) pre zobrazenie dôležitých dátumov."""
    badge_html = ""
    if badge_label is not None and badge_val is not None:
        badge_html = format_html(
            '<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 800; border: 1px solid #e2e8f0; display: block; margin-top: 6px;">{}: {}</span>',
            badge_label, badge_val
        )
    safe_primary = "—" if primary_val is None or str(primary_val).strip() == "" else primary_val
    safe_secondary = "—" if secondary_val is None or str(secondary_val).strip() == "" else secondary_val
    
    return format_html(
        '<div style="display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; width: 100%;">'
        '<span style="color: #475569; font-size: 11px; font-weight: 700;">{}: <b style="color:#0f172a;">{}</b></span>'
        '<span style="color: #64748b; font-size: 10px; font-weight: 600; margin-top: 2px;">{}: {}</span>'
        '{}'
        '</div>',
        primary_label or "", safe_primary, secondary_label or "", safe_secondary, mark_safe(badge_html)
    )

def render_isas_progress_bar(label_left, label_right, value, max_value, subtext=""):
    """Generuje vizuálny progress bar (napr. pre čerpanie dovolenky)."""
    try:
        val_str = str(value or '0').strip().replace(',', '.')
        safe_val = Decimal(val_str) if val_str else Decimal('0.00')
        if not safe_val.is_finite():
            safe_val = Decimal('0.00')
    except (ValueError, TypeError, Exception):
        safe_val = Decimal('0.00')

    try:
        max_str = str(max_value or '0').strip().replace(',', '.')
        safe_max = Decimal(max_str) if max_str else Decimal('0.00')
        if not safe_max.is_finite():
            safe_max = Decimal('0.00')
    except (ValueError, TypeError, Exception):
        safe_max = Decimal('0.00')

    percent = (safe_val / safe_max * Decimal('100.00')) if safe_max > Decimal('0.00') else Decimal('0.00')
    percent = min(max(percent, Decimal('0.00')), Decimal('100.00')).quantize(Decimal('0.01'))
    
    if percent < Decimal('50.00'):
        color = "#10b981"
    elif percent < Decimal('85.00'):
        color = "#f59e0b"
    else:
        color = "#ef4444"
        
    return format_html(
        '<div style="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%;">'
        '<div style="width: 100%; min-width: 120px; max-width: 200px; font-family: sans-serif;">'
        '<div style="display: flex; justify-content: space-between; gap: 8px; font-size: 10px; font-weight: 800; color: #475569; margin-bottom: 4px; white-space: nowrap;">'
        '<span>{}</span><span>{}</span>'
        '</div>'
        '<div style="width: 100%; background: #e2e8f0; border-radius: 10px; height: 8px; overflow: hidden; box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);">'
        '<div style="width: {}%; background: {}; height: 100%; border-radius: 10px; transition: width 0.5s ease;"></div>'
        '</div>'
        '<div style="font-size: 9px; color: #94a3b8; margin-top: 4px; text-align: right;">{}</div>'
        '</div></div>',
        mark_safe(label_left or ""), mark_safe(label_right or ""), percent, color, subtext or ""
    )

def render_isas_icon_stat_card(icon, label, value, icon_bg, value_color="#0f172a", suffix=""):
    """Horizontálna štatistická karta s ikonou naľavo (Dashboard item)."""
    suffix_html = format_html('<small style="font-size:14px; margin-left:4px;">{}</small>', suffix) if suffix else ""
    return format_html(
        '<div style="display: flex; align-items: center; gap: 12px; background: white; padding: 10px 16px; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 2px 4px rgba(0,0,0,0.02); flex: 1; min-width: 200px; overflow: hidden;">'
        '<div style="width: 38px; height: 38px; border-radius: 10px; background: {}; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; color: white; box-shadow: inset 0 -2px 4px rgba(0,0,0,0.1);">{}</div>'
        '<div style="display: flex; flex-direction: column; overflow: hidden; width: 100%;">'
        '<span style="font-size: 10px; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{}</span>'
        '<span style="font-size: 16px; font-weight: 900; color: {};">{}{}</span>'
        '</div></div>',
        mark_safe(icon_bg), mark_safe(icon), label or "", value_color, value if value is not None else "0", mark_safe(suffix_html)
    )

# --- 5. POMOCNÉ UTILITY ---

def render_isas_text(text, size=11, color="#475569", weight=700, align="center", max_width=None, icon=None, muted=False, tooltip=None, dot_color=None, custom_style=None, css_class=None, data_attrs=None):
    if muted:
        color = "#94a3b8"
        weight = 400
        font_style = "italic"
    else:
        font_style = "normal"
        
    try:
        clean_size = int(re.sub(r'[^\d]', '', str(size))) if size else 11
    except (ValueError, TypeError):
        clean_size = 11
    mw_style = f"max-width: {max_width}px;" if max_width else ""
    icon_html = format_html('<span style="margin-right: 6px; font-size: {}px;">{}</span>', clean_size + 2, mark_safe(icon)) if icon else ""
    
    dot_html = format_html('<span style="width: 10px; height: 10px; border-radius: 50%; background-color: {}; box-shadow: 0 1px 3px rgba(0,0,0,0.2); margin-right: 4px; display: inline-block;"></span>', dot_color) if dot_color else ""
    
    tooltip_attr = format_html(' title="{}"', tooltip) if tooltip else ""
    cursor_style = " cursor: help;" if tooltip else ""
    
    class_attr = format_html(' class="{}"', css_class) if css_class else ""
    data_html = ""
    if isinstance(data_attrs, dict):
        data_html = mark_safe(" ".join([f'data-{escape(k)}="{escape(v)}"' for k, v in data_attrs.items()]))
    
    justify_val = "center" if align == "center" else ("flex-end" if align == "right" else "flex-start")
    style_str = f"font-size: {clean_size}px; color: {color}; font-weight: {weight}; font-style: {font_style}; text-align: {align}; line-height: 1.3; display: inline-flex; align-items: center; justify-content: {justify_val}; {mw_style}{cursor_style}"
    if custom_style:
        style_str += f" {custom_style}"
    
    safe_text = "" if text is None else text
    return format_html(
        '<div{}{}{} style="{}">{}</div>', 
        class_attr, format_html(' {}', data_html) if data_html else "", tooltip_attr, mark_safe(style_str), format_html('{}{}{}', mark_safe(dot_html), mark_safe(icon_html), safe_text)
    )

def render_isas_hidden_data(css_class, data_id):
    """Generuje neviditeľný span pre JS dáta (napr. pre custom akcie)."""
    return format_html('<span class="{}" style="display:none;" data-id="{}"></span>', css_class, data_id)

def render_isas_image(url, alt="", width=38, height=38, custom_style=None):
    if not url:
        return ""
    extra_style = f" {custom_style}" if custom_style else ""
    return format_html(
        '<img src="{}" width="{}" height="{}" style="border: 1px solid #cbd5e1; border-radius: 6px; background: white; object-fit: contain; padding: 2px; flex-shrink: 0;{}" alt="{}" />',
        url, width, height, extra_style, alt
    )

def render_isas_script(script_content=None, src=None):
    """Bezpečné vloženie JavaScript kódu (napr. pre custom admin akcie) alebo externého skriptu."""
    if src:
        return format_html('<script src="{}"></script>', src)
    if script_content:
        return format_html('<script>\n{}\n</script>', mark_safe(script_content))
    return ""

def render_isas_style(style_content):
    """Bezpečné vloženie CSS štýlov (napr. pre custom admin modaly)."""
    return format_html('<style>\n{}\n</style>', mark_safe(style_content))

def render_isas_animated_banner(title, scene_html):
    """Generuje animovaný banner (napr. ISAS CORE workshop). Vyžaduje prítomnosť špecifických CSS tried."""
    return format_html(
        '<div class="isas-core-workshop">'
        '<span class="workshop-title">{}</span>'
        '<div class="workshop-scene">{}</div>'
        '</div>',
        title, mark_safe(scene_html)
    )

def render_isas_hero_dashboard_card(icon, title, subtitle, status_text, button_text, button_url, button_icon=""):
    """Generuje veľkolepú vstupnú kartu (Hero Card) s animáciami. Vyžaduje prítomnosť špecifických CSS tried."""
    return format_html(
        '<div class="premium-sklad-card">'
        '<div class="sklad-left-section">'
        '<div class="sklad-icon-box">{}</div>'
        '<div class="sklad-text-content">'
        '<div class="sklad-title">{}</div>'
        '<div class="sklad-subtitle">{}</div>'
        '</div>'
        '</div>'
        '<div class="sklad-right-section">'
        '<div class="sklad-status"><div class="status-dot"></div> {}</div>'
        '<a href="{}" class="sklad-btn-vstup">{} <span class="sklad-btn-icon">{}</span></a>'
        '</div>'
        '</div>',
        mark_safe(icon), title, subtitle, status_text, button_url or "#", button_text, mark_safe(button_icon)
    )