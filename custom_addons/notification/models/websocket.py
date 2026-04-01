# import json
# import threading
# import asyncio
# import logging
# from websockets.client import connect
# from odoo import models, api
#
# _logger = logging.getLogger(__name__)
#
# async def send_websocket_message(self, text: dict, room=None):
#     server = self.env['ir.config_parameter'].sudo().get_param('websocket.server_url')
#     qs = f"?room={room}&name=odoo-backend"
#     try:
#         async with connect(server + qs, ping_interval=20, ping_timeout=20) as ws:
#             await ws.send(json.dumps({
#                 "type": "response",
#                 "text": text
#             }))
#     except Exception as e:
#         _logger.exception(f"WebSocket send failed: {e}")
#
# def trigger_async_ws_message(self, text: dict, room: str):
#     def _run():
#         asyncio.run(send_websocket_message(self, text, room=room))
#     threading.Thread(target=_run, daemon=True).start()
#
