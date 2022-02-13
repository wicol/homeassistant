"""Support for Telegram bots using webhooks."""
import datetime as dt
from http import HTTPStatus
from ipaddress import ip_address
import logging

from telegram import Update
from telegram.error import TimedOut
from telegram.ext import Dispatcher

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.network import get_url

from . import CONF_TRUSTED_NETWORKS, CONF_URL, BaseTelegramBotEntity

_LOGGER = logging.getLogger(__name__)

TELEGRAM_WEBHOOK_URL = "/api/telegram_webhooks"
REMOVE_WEBHOOK_URL = ""


async def async_setup_platform(hass, bot, config):
    """Set up the Telegram webhooks platform."""

    pushbot = PushBot(hass, bot, config)

    if not pushbot.webhook_url.startswith("https"):
        _LOGGER.error("Invalid telegram webhook %s must be https", pushbot.webhook_url)
        return False

    webhook_registered = await pushbot.register_webhook()
    if not webhook_registered:
        return False

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, pushbot.deregister_webhook)
    hass.http.register_view(
        PushBotView(bot, pushbot.dispatcher, config[CONF_TRUSTED_NETWORKS])
    )
    return True


class PushBot(BaseTelegramBotEntity):
    """Handles all the push/webhook logic and passes telegram updates to `self.handle_update`."""

    def __init__(self, hass, bot, config):
        """Create Dispatcher before calling super()."""
        self.bot = bot
        # Dumb dispatcher that just gets our updates to our handler callback (self.handle_update)
        self.dispatcher = Dispatcher(bot, None, workers=0)
        self.trusted_networks = config[CONF_TRUSTED_NETWORKS]
        super().__init__(hass, config)

        self.base_url = config.get(CONF_URL) or get_url(
            hass, require_ssl=True, allow_internal=False
        )
        self.webhook_url = f"{self.base_url}{TELEGRAM_WEBHOOK_URL}"

    def _try_to_set_webhook(self):
        _LOGGER.debug("Registering webhook URL: %s", self.webhook_url)
        retry_num = 0
        while retry_num < 3:
            try:
                return self.bot.set_webhook(self.webhook_url, timeout=5)
            except TimedOut:
                retry_num += 1
                _LOGGER.warning("Timeout trying to set webhook (retry #%d)", retry_num)

    async def register_webhook(self):
        """Query telegram and register the URL for our webhook."""
        current_status = await self.hass.async_add_executor_job(
            self.bot.get_webhook_info
        )
        # Some logging of Bot current status:
        last_error_date = getattr(current_status, "last_error_date", None)
        if (last_error_date is not None) and (isinstance(last_error_date, int)):
            last_error_date = dt.datetime.fromtimestamp(last_error_date)
            _LOGGER.info(
                "Telegram webhook last_error_date: %s. Status: %s",
                last_error_date,
                current_status,
            )
        else:
            _LOGGER.debug("telegram webhook Status: %s", current_status)

        if current_status and current_status["url"] != self.webhook_url:
            result = await self.hass.async_add_executor_job(self._try_to_set_webhook)
            if result:
                _LOGGER.info("Set new telegram webhook %s", self.webhook_url)
            else:
                _LOGGER.error("Set telegram webhook failed %s", self.webhook_url)
                return False

        return True

    def deregister_webhook(self, event=None):
        """Query telegram and deregister the URL for our webhook."""
        _LOGGER.debug("Deregistering webhook URL")
        return self.bot.delete_webhook()


class PushBotView(HomeAssistantView):
    """View for handling webhook calls from Telegram."""

    requires_auth = False
    url = TELEGRAM_WEBHOOK_URL
    name = "telegram_webhooks"

    def __init__(self, bot, dispatcher, trusted_networks):
        """Initialize by storing stuff needed for setting up our webhook endpoint."""
        self.bot = bot
        self.dispatcher = dispatcher
        self.trusted_networks = trusted_networks

    async def post(self, request):
        """Accept the POST from telegram."""
        real_ip = ip_address(request.remote)
        if not any(real_ip in net for net in self.trusted_networks):
            _LOGGER.warning("Access denied from %s", real_ip)
            return self.json_message("Access denied", HTTPStatus.UNAUTHORIZED)

        try:
            update_data = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON", HTTPStatus.BAD_REQUEST)

        update = Update.de_json(update_data, self.bot)
        _LOGGER.debug("Received Update on %s: %s", self.url, update)
        self.dispatcher.process_update(update)

        return None
