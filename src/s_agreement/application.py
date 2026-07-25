"""S-Agreement manifest and host wiring."""

from sovereign import (
    ApplicationFacade, ApplicationInstance, ApplicationManifest,
    ApplicationServices,
)

from .controller import build_routes
from .facade import AGREEMENT_FACADE_API_VERSION, AgreementFacade
from .logic import AgreementLogic


APPLICATION_MANIFEST = ApplicationManifest(
    application_id="agreement",
    display_name="S-Agreement",
    data_schema_version=1,
    asset_package="s_agreement.assets",
    icon=(
        '<path d="M6 3h8l4 4v14H6z"></path>'
        '<path d="M14 3v4h4"></path><path d="M9 13h6"></path>'
        '<path d="M9 17h6"></path>'
    ),
    ui_file="agreement.html",
    css_file="agreement.css",
)


def create_application(services: ApplicationServices) -> ApplicationInstance:
    logic = AgreementLogic(
        services.session,
        dict(services.settings),
        services.collaboration,
    )
    return ApplicationInstance(
        manifest=APPLICATION_MANIFEST,
        logic=logic,
        registration=logic.application_registration(),
        controllers=tuple(build_routes(logic, services, dict(services.settings))),
        facade=ApplicationFacade(
            application_id=APPLICATION_MANIFEST.application_id,
            facade_api_version=AGREEMENT_FACADE_API_VERSION,
            api=AgreementFacade(logic),
        ),
    )
