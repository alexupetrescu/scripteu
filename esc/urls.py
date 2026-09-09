from django.urls import path

from . import views

app_name = 'esc'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    path('profiles/new/', views.profile_edit, name='profile_new'),
    path('profiles/<int:pk>/', views.profile_edit, name='profile_edit'),
    path('profiles/<int:pk>/delete/', views.profile_delete, name='profile_delete'),
    path('profiles/<int:pk>/run/', views.run_start, name='run_start'),

    path('runs/', views.run_list, name='run_list'),
    path('runs/<int:pk>/', views.run_detail, name='run_detail'),
    path('runs/<int:pk>/status.json', views.run_status, name='run_status'),
    path('runs/<int:pk>/stop/', views.run_stop, name='run_stop'),

    path('outreach/', views.outreach_log, name='outreach_log'),
    path('outreach/<int:pk>/resolve/', views.outreach_resolve, name='outreach_resolve'),
    path('candidates/', views.candidate_list, name='candidate_list'),

    path('templates/', views.template_list, name='template_list'),
    path('templates/new/', views.template_edit, name='template_new'),
    path('templates/<int:pk>/', views.template_edit, name='template_edit'),

    path('settings/', views.settings_view, name='settings'),
    path('settings/toggle-pause/', views.toggle_pause, name='toggle_pause'),
    path('settings/login/', views.portal_login, name='portal_login'),
    path('settings/login/status.json', views.portal_login_status, name='portal_login_status'),
]
