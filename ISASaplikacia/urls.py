from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

from ISASaplikacia.views import isas_mail_api, crm_intel_api, api_live_radar, spotify_login, spotify_callback, api_spotify_token, api_history_radar, api_optimize_route, api_easter_egg_2048, protected_media_view

admin.site.empty_value_display = ""

def dynamic_root_redirect(request):
    """🛡️ Subdoménu klienta presmeruje na IoT Dashboard, hlavnú doménu do ERP Adminu."""
    if getattr(request, 'is_subdomain', False):
        return redirect('/evidencia/iot-dashboard/', permanent=False)
    return redirect('/admin/', permanent=False)

urlpatterns = [
    # Explicitné lomítko na začiatku bráni chybnému zreťazeniu relatívnych URL za reverse proxy
    path('', dynamic_root_redirect),
    # 🎮 ISAS 2048: Musí byť PRED admin.site.urls, inak admin zachytí požiadavku a vráti 404
    path('admin/api/easter-egg/2048/', api_easter_egg_2048, name='api_easter_egg_2048'),
    path('api/easter-egg/2048/', api_easter_egg_2048),
    path("admin/", admin.site.urls),
    path("_nested_admin/", include("nested_admin.urls")),
    path("evidencia/", include("Evidencia.urls")),
    path("isas-mail-api/", isas_mail_api, name="isas_mail_api"),
    path('api/crm-intel/', crm_intel_api, name='crm_intel_api'),
    path('api/live-radar/', api_live_radar, name='api_live_radar'),
    path('api/history-radar/', api_history_radar, name='api_history_radar'),
    path('api/optimize-route/', api_optimize_route, name='api_optimize_route'),
    path('spotify/login/', spotify_login, name='spotify_login'),
    path('spotify/callback/', spotify_callback, name='spotify_callback'),
    path('api/spotify/token/', api_spotify_token, name='api_spotify_token'),
    re_path(
        r'^media/(?P<rel_path>(?:systemove_zalohy|systemove_exporty|importy_dat|hr_zmluvy|hr_certifikaty|hr_absencie|kariera/zivotopisy|emailove_prilohy|emaile_prilohy|objednavky_dodavatelom|faktury|faktury_xml|faktury_vizualizacie|blocky|vygenerovane_vstupy|hr_podpisy|identita/peciatky|vystavene_faktury_pdf|peppol_xml|cenove_ponuky_pdf|prijate_objednavky|servisne_protokoly_pdf|sablony_vstupov)/.+)$',
        protected_media_view,
        name='protected_media',
    ),
    path('', include('WebBuilder.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)