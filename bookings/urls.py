from django.urls import path
from . import views
from . import auth_views
from . import manage_views

urlpatterns = [
    path('health/', views.HealthView.as_view(), name='health'),
    path('companies/<slug:slug>/masters/', views.MasterListView.as_view()),
    path('companies/<slug:slug>/services/', views.ServiceListView.as_view()),
    path('companies/<slug:slug>/appointments/', views.AppointmentCreateView.as_view()),
    path('companies/<slug:slug>/slots/', views.SlotListView.as_view()),
    path('companies/<slug:slug>/points/', views.SelectData.as_view()),
    path('companies/<slug:slug>/calendar/', views.CalendarView.as_view()),
    path('companies/<slug:slug>/nearest/', views.NearestSlotsView.as_view()),
    path('companies/<slug:slug>/visit-slots/', views.VisitSlotsView.as_view()),
    path('companies/<slug:slug>/visits/', views.VisitCreateView.as_view()),
    path('manage/<str:token>/', manage_views.ManageAppointmentView.as_view()),
    path('manage/<str:token>/cancel/', manage_views.ManageCancelView.as_view()),
    path('manage/<str:token>/slots/', manage_views.ManageSlotsView.as_view()),
    path('manage/<str:token>/reschedule/', manage_views.ManageRescheduleView.as_view()),
    path('companies/<slug:slug>/info/', views.CompanyInfoView.as_view()),
    path('companies/<slug:slug>/master/<int:master_id>/reviews/', views.ReviewView.as_view()),
    path('companies/<slug:slug>/master/<int:master_id>/services/', views.MasterServices.as_view()),
    path('appointments/my/', auth_views.MyAppointmentView.as_view()),
    path('companies/<slug:slug>/my-debt/', auth_views.MyDebtView.as_view()),
    path('appointments/<int:appointment_id>/cancel/', auth_views.CancelAppointmentsView.as_view()),
    path('auth/register/', auth_views.RegisterView.as_view(), name='register'),
    path('auth/login/', auth_views.LoginView.as_view(), name='login'),
    path('auth/me/', auth_views.MeView.as_view(), name='me'),
]
