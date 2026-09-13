from django.urls import path

from . import views

app_name = "wallet"

urlpatterns = [
    path("", views.WalletDetailView.as_view(), name="detail"),
    path("transactions/", views.TransactionListView.as_view(), name="transactions"),
    path("deposit/", views.CreateDepositView.as_view(), name="deposit"),
    path("withdraw/", views.CreateWithdrawalView.as_view(), name="withdraw"),
]
