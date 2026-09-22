# Crea (o reutiliza) una cuenta de servicio y le emite un personal access token.
#
# Se corre dentro del contenedor, con el shell de Django:
#   docker compose exec -T -e CUENTA=scripts@hub.local -e NOMBRE="Scripts" \
#     app python label_studio/manage.py shell < cuenta_servicio.py
#
# Imprime solo el token, en la última línea. Por qué cuentas aparte y no el admin:
# Label Studio guarda `completed_by` de cada anotación con el usuario que la
# importó. Con una cuenta por origen ("migración", "BirdNET") la procedencia
# queda visible en la interfaz, en vez de parecer que el admin etiquetó 15 000
# fotos. Los tokens legacy siguen apagados: estos son JWT.
import os

from django.contrib.auth import get_user_model
from jwt_auth.models import LSAPIToken
from organizations.models import Organization
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

email = os.environ["CUENTA"]
nombre = os.environ.get("NOMBRE", email.split("@")[0])

User = get_user_model()
org = Organization.objects.order_by("id").first()
user, creada = User.objects.get_or_create(email=email, defaults={"username": email})
if creada:
    user.set_unusable_password()  # sin login por la web
user.first_name = nombre
user.active_organization = org
user.save()
org.add_user(user)

# Un solo token vivo por cuenta: se revocan los anteriores antes de emitir.
for t in OutstandingToken.objects.filter(user=user):
    try:
        LSAPIToken(t.token, verify=False).blacklist()
    except Exception:
        pass
print(LSAPIToken.for_user(user).get_full_jwt())
