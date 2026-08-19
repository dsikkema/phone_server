from typing import Union
import urllib.parse 
import httpx
import logging
from cryptography.fernet import Fernet
import json
from pydantic import BaseModel, ValidationError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from datetime import datetime
import os

# instruction.json

# lookup_addr.json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.exception_handler(ValidationError)
async def handle_validation_err(request: Request, exc: ValidationError):
    logging.error("caugt exc: %s", exc)
    return JSONResponse(
            status_code=400,
            content=jsonable_encoder({'detail': 'invalid request'})
            )

class OuterPayload(BaseModel):
    enc_content: str

class OuterResponse(BaseModel):
    enc_response: str

class RouterPayload(BaseModel):
    path: str
    body: dict

class AddressLookupReq(BaseModel):
    address: str

class AddressLookupResp(BaseModel):
    latitude: float
    longitude: float
    full_address: str

class NavigationRequest(BaseModel):
    fromLat: float
    fromLong: float
    to_address: str

class NavigationResponse(BaseModel):
    # do <instr> then go <distance> meters
    steps: list[tuple[str, float]]
    resolved_address: str

def get_mapbox_token():
    tok = os.environ.get("MAPBOX_TOKEN")
    if not tok:
        raise HTTPException(status_code=500, detail='missing/invalid mapbox token')
    return tok

async def lookup_address(req: dict):
    # https://api.mapbox.com/search/geocode/v6/forward?q=3500+Cookstown+Dr+Austin+Tx&access_token=
    lookup_req = AddressLookupReq.model_validate(req)
    mapbox_tok = get_mapbox_token()
    addr = urllib.parse.quote_plus(lookup_req.address)

    url = f"https://api.mapbox.com/search/geocode/v6/forward?limit=1&q={addr}&access_token={mapbox_tok}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logging.error("got status %s from mapbox", resp.status_code)
            raise HTTPException(status_code=500, detail='error getting addr lookup')
    except:
        logging.exception("error calling mapbox")
        raise HTTPException(status_code=500, detail='error getting addr lookup')


    resp_ob = json.loads(resp.text)
    coord = resp_ob['features'][0]['properties']['coordinates']
    resp_addr = resp_ob['features'][0]['properties']['full_address']
    try: 
        return AddressLookupResp.model_validate(
            {
                    "latitude": coord['latitude'],
                    "longitude": coord['longitude'],
                    "full_address": resp_addr
            }    
        )
    except ValidationError as e:
        logging.exception("error decoding nav resp")
        raise HTTPException(status_code=500, detail='failed to decode nav response')

async def navigation_directions(req: dict):
    # http://api.mapbox.com/directions/v5/mapbox/driving/-97.71382563%2C30.4186%3B-97.7179538%2C30.397959?steps=true&access_token=
    # Note: exclude=toll
    # overview=false
    # geometries=false

    with open('../instruction2.json', 'r') as f:
        instr = f.read()
        assert len(instr) > 0
    nav_req = NavigationRequest.model_validate(req)

    # lookup the coordinates for the searched-for address using the dedicated method for that
    addr_lookup = await lookup_address({"address": nav_req.to_address})
    to_long = addr_lookup.longitude
    to_lat = addr_lookup.latitude
    resolved_address = addr_lookup.full_address

    base_url = f"https://api.mapbox.com/directions/v5/mapbox/driving"
    url =   f"{base_url}/{nav_req.fromLong}%2C{nav_req.fromLat}%3B{to_long}%2C{to_lat}" + \
            f"?exclude=toll&overview=false&steps=true&access_token={get_mapbox_token()}"

    # routes[0].legs[0].steps[] | .distince, .maneuver.instruction
    try:
        if False:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                logging.error("got status %s from mapbox", resp.status_code)
                raise HTTPException(status_code=500, detail='error getting nav directions')
            logger.info('resp: %s', resp.text)
        else:
            resp = lambda: ...
            resp.text = instr
    except:
        logger.exception("error calling mapbox")
        raise HTTPException(status_code=500, detail='error getting nav directions')
    try:
        steps = json.loads(resp.text)['routes'][0]['legs'][0]['steps']
        # do <instr> then go <distance> meters
        fmtd_steps = [(step['maneuver']['instruction'], step['distance']) for step in steps]
        return NavigationResponse.model_validate({'steps': fmtd_steps, 'resolved_address': resolved_address})
    except:
        logger.exception('failed to parse nav output: %s', resp.text)
        raise HTTPException(status_code=500, detail='cannot parse nav output')

async def route(payload: RouterPayload):
    routing_table = {
        'lookup_address': lookup_address,
        'navigation_directions': navigation_directions,
    }
    fn = routing_table[payload.path]
    return await fn(payload.body)

@app.post("/")
async def routed_request_endpoint(body: OuterPayload):
    """
    >>> from cryptography.fernet import Fernet
    >>> # Put this somewhere safe!
    >>> key = Fernet.generate_key()
    >>> f = Fernet(key)
    >>> token = f.encrypt(b"A really secret message. Not for prying eyes.")
    >>> token
    b'...'
    >>> f.decrypt(token)
    b'A really secret message. Not for prying eyes.'
    """
    key = os.environ.get('FERMET_KEY')
    if not key:
        raise HTTPException(status_code=500)

    try:
        fernet = Fernet(key.encode())
        plain = fernet.decrypt(body.enc_content.encode())
        plain_dict = json.loads(plain)
        payload = RouterPayload.model_validate(plain_dict)
    except Exception as e:
        logger.exception('error parsing req')
        raise HTTPException(status_code=400, detail='failed decoding body')
    try:
        resp = await route(payload)
        return OuterResponse.model_validate({"enc_response": fernet.encrypt(resp.model_dump_json().encode())})
    except Exception as e:
        logger.exception('error formatting output')
        raise HTTPException(status_code=400, detail='failed encoding output')
@app.get("/health")
async def health():
    return {"status": "ok"}


