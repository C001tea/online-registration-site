from django.urls import path

from . import views

urlpatterns = [
    path('summary/', views.SummaryView.as_view(), name='finance-summary'),
    path('cash/', views.CashDayView.as_view(), name='finance-cash'),
    path('operations/', views.OperationsView.as_view(), name='finance-operations'),
    path('operations/<int:pk>/', views.OperationDetailView.as_view(), name='finance-operation'),
    path('salaries/', views.SalariesView.as_view(), name='finance-salaries'),
    path('salaries/<int:master_id>/scheme/', views.SchemeView.as_view(), name='finance-scheme'),
    path('salaries/<int:master_id>/payouts/', views.PayoutsView.as_view(), name='finance-payouts'),
    path('payouts/<int:pk>/', views.PayoutDetailView.as_view(), name='finance-payout'),
    path('my-salary/', views.MySalaryView.as_view(), name='finance-my-salary'),
]
