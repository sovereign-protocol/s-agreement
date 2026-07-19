"""S-Agreement manifest and host wiring."""

from sovereign import ApplicationInstance, ApplicationManifest, ApplicationServices

from .controller import build_routes
from .logic import AgreementLogic


APPLICATION_MANIFEST = ApplicationManifest(
    application_id="agreement",
    display_name="S-Agreement",
    data_schema_version=1,
    asset_package="s_agreement.assets",
    ui_file="agreement.html",
    css_file="agreement.css",
)


def create_application(services: ApplicationServices) -> ApplicationInstance:
    logic = AgreementLogic(
        services.session,
        dict(services.settings),
        services.channel_manager,
    )
    return ApplicationInstance(
        manifest=APPLICATION_MANIFEST,
        logic=logic,
        registration=logic.application_registration(),
        controllers=tuple(build_routes(logic, services, dict(services.settings))),
    )
