from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='evidencia_index'),
]
