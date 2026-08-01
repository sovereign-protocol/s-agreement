"""S-Team manifest and host wiring."""

from sovereign import (
    ApplicationFacade, ApplicationInstance, ApplicationManifest,
    ApplicationServices,
)

from .controller import build_routes
from .facade import TEAM_FACADE_API_VERSION, TeamFacade
from .logic import TeamLogic


APPLICATION_MANIFEST = ApplicationManifest(
    application_id="team",
    display_name="S-Team",
    data_schema_version=3,
    asset_package="s_team.assets",
    icon=(
        '<path d="M6 3h8l4 4v14H6z"></path>'
        '<path d="M14 3v4h4"></path><path d="M9 13h6"></path>'
        '<path d="M9 17h6"></path>'
    ),
    ui_file="team.html",
    css_file="team.css",
)


def create_application(services: ApplicationServices) -> ApplicationInstance:
    logic = TeamLogic(
        services.session,
        dict(services.settings),
        services.collaboration,
    )
    return ApplicationInstance(
        manifest=APPLICATION_MANIFEST,
        logic=logic,
        registration=logic.application_registration(),
        controllers=tuple(build_routes(logic, services)),
        facade=ApplicationFacade(
            application_id=APPLICATION_MANIFEST.application_id,
            facade_api_version=TEAM_FACADE_API_VERSION,
            api=TeamFacade(logic),
        ),
    )
