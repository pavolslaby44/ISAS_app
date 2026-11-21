from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.sitemaps.views import sitemap
from django.http import HttpResponse
from . import views
from .sitemaps import StaticViewSitemap, SluzbySitemap, ReferencieSitemap, RentalSitemap, EshopSitemap, KarieraSitemap, BlogSitemap

sitemaps = {
    'static': StaticViewSitemap,
    'sluzby': SluzbySitemap,
    'referencie': ReferencieSitemap,
    'rental': RentalSitemap,
    'eshop': EshopSitemap,
    'kariera': KarieraSitemap,
    'blog': BlogSitemap,
}

app_name = 'webbuilder'

_web_patterns = [
    path('favicon.ico', lambda request: HttpResponse(status=204)),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    
    path('', views.public_web_view, name='home'),
    
    # --- FÁZA 2: MPA TRASY (Katalógy a Detaily) ---
    path('sluzby/', views.SluzbyListView.as_view(), name='sluzby_list'),
    path('sluzby/<slug:slug>/', views.SluzbaDetailView.as_view(), name='sluzba_detail'),
    
    path('referencie/', views.ReferencieListView.as_view(), name='referencie_list'),
    path('referencie/<slug:slug>/', views.ReferenciaDetailView.as_view(), name='referencia_detail'),

    path('blog/', views.ClanokListView.as_view(), name='blog_list'),
    path('blog/<slug:slug>/', views.ClanokDetailView.as_view(), name='blog_detail'),
    
    path('pozicovna/', views.RentalListView.as_view(), name='rental_list'),
    path('pozicovna/<slug:slug>/', views.RentalDetailView.as_view(), name='rental_detail'),
    
    path('eshop/', views.EshopListView.as_view(), name='eshop_list'),
    path('eshop/<slug:slug>/', views.EshopDetailView.as_view(), name='eshop_detail'),

    path('nastroje/', views.NastrojeView.as_view(), name='nastroje'),
    path('reklamacie/', views.ReklamaciaView.as_view(), name='reklamacie'),
    
    path('kariera/', views.KarieraListView.as_view(), name='kariera_list'),
    path('kariera/<slug:slug>/', views.KarieraDetailView.as_view(), name='kariera_detail'),
    # ----------------------------------------------

    # --- FÁZA 4: B2B KLIENTSKA ZÓNA ---
    path('b2b/login/', views.b2b_login_view, name='b2b_login'),
    path('b2b/logout/', views.b2b_logout_view, name='b2b_logout'),
    path('b2b/dashboard/', views.b2b_dashboard_view, name='b2b_dashboard'),
    # ----------------------------------------------

    path('dopyt/', views.submit_inquiry, name='submit_inquiry'),
    path('api/check-ticket/', views.check_ticket_status, name='check_ticket_status'),
    path('api/calculate-fee/', views.calculate_callout_fee, name='calculate_callout_fee'),
    path('api/public-iot-demo/', views.public_iot_demo, name='public_iot_demo'),
    path('api/generate-esg-audit/', views.generate_esg_audit, name='generate_esg_audit'),
    path('api/submit-career/', views.submit_career, name='submit_career'),
    path('api/submit-special-lead/', views.submit_special_lead, name='submit_special_lead'),
    path('api/booking/slots/', views.get_available_slots, name='get_available_slots'),
    path('api/booking/submit/', views.submit_booking, name='submit_booking'),
    path('zariadenie/<uuid:kluc>/', views.machine_passport_view, name='machine_passport'),
]

# 🛡️ Dvojitá registrácia: Zabezpečí funkčnosť priameho volania aj namespace 'webbuilder:'
urlpatterns = [
    path('', include((_web_patterns, 'webbuilder'), namespace='webbuilder')),
] + _web_patterns

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)