from django.urls import path
from . import views
from . import auth_views

urlpatterns = [
    path('companies/<slug:slug>/masters/', views.MasterListView.as_view()),
    path('companies/<slug:slug>/services/', views.ServiceListView.as_view()),
    path('companies/<slug:slug>/appointments/', views.AppointmentCreateView.as_view()),
    path('companies/<slug:slug>/slots/', views.SlotListView.as_view()),
    path('companies/<slug:slug>/info/', views.CompanyInfoView.as_view()),
    path('companies/<slug:slug>/master/<int:master_id>/reviews/', views.ReviewView.as_view()),
    path('appointments/my/', auth_views.MyAppointmentView.as_view()),
    path('appointments/<int:appointment_id>/cancel/', auth_views.CancelAppointmentsView.as_view()),
    path('auth/register/', auth_views.RegisterView.as_view(), name='register'),
    path('auth/login/', auth_views.LoginView.as_view(), name='login')
]