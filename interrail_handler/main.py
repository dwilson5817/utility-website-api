from mangum import Mangum

from interrail import app

# AWS Lambda entry point. Configure the function's handler as ``main.handler``
# (this file sits at the root of the deployment zip). Mangum adapts the ASGI app
# to the Lambda/API Gateway calling convention.
#
# The API layer maps ``/interrail`` to this function; ``api_gateway_base_path``
# makes Mangum strip that prefix so the app sees ``/manifest`` etc. It's a no-op
# if the gateway already strips the base path, so both mapping styles work.
handler = Mangum(app, api_gateway_base_path="/interrail")
