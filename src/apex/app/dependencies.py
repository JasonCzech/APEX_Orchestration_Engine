from typing import Annotated

from fastapi import Depends

from apex.settings import ApexSettings, get_settings

SettingsDep = Annotated[ApexSettings, Depends(get_settings)]
