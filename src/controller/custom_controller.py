import asyncio
import subprocess
import json
import pyperclip
import uuid
from typing import Optional, Type
from pydantic import BaseModel
from browser_use.agent.views import ActionResult
from browser_use.browser.context import BrowserContext
from browser_use.controller.service import Controller, DoneAction
from main_content_extractor import MainContentExtractor
from browser_use.controller.views import (
    ClickElementAction,
    DoneAction,
    ExtractPageContentAction,
    GoToUrlAction,
    InputTextAction,
    OpenTabAction,
    ScrollAction,
    SearchGoogleAction,
    SendKeysAction,
    SwitchTabAction,
)
import logging
import os

logger = logging.getLogger(__name__)

class SendTwilioMessageAction(BaseModel):
    message: str

class VerifyTwilioMessageAction(BaseModel):
    expected_message: Optional[str] = None

class CustomController(Controller):
    def __init__(self, exclude_actions: list[str] = [],
                 output_model: Optional[Type[BaseModel]] = None
                 ):
        super().__init__(exclude_actions=exclude_actions, output_model=output_model)
        self._register_custom_actions()
        self.last_sent_message = None

    def _register_custom_actions(self):
        """Register all custom browser actions"""

        @self.registry.action("Copy text to clipboard")
        def copy_to_clipboard(text: str):
            pyperclip.copy(text)
            return ActionResult(extracted_content=text)

        @self.registry.action("Paste text from clipboard", requires_browser=True)
        async def paste_from_clipboard(browser: BrowserContext):
            text = pyperclip.paste()
            page = await browser.get_current_page()
            await page.keyboard.type(text)
            return ActionResult(extracted_content=text)

        @self.registry.action(
            'Extract page content to get the pure text or markdown with links if include_links is set to true',
            param_model=ExtractPageContentAction,
            requires_browser=True,
        )
        async def extract_content(params: ExtractPageContentAction, browser: BrowserContext):
            page = await browser.get_current_page()
            url = page.url
            jina_url = f"https://r.jina.ai/{url}"
            await page.goto(jina_url)
            output_format = 'markdown' if params.include_links else 'text'
            content = MainContentExtractor.extract(
                html=await page.content(),
                output_format=output_format,
            )
            await page.go_back()
            msg = f'Extracted page content:\n {content}\n'
            logger.info(msg)
            return ActionResult(extracted_content=msg)

        # Overwriting the default "Done" action
        @self.registry.action("Done", param_model=DoneAction)
        async def done(params: DoneAction):
            print("Completing task and sending WhatsApp notification...")
            
            done_message = SendTwilioMessageAction(message=params.text)
            send_result = await send_twilio_message(done_message)
            
            return ActionResult(
                is_done=True, 
                extracted_content=f"Task completed: {params.text}"
            )

        @self.registry.action("Send Twilio Message", param_model=SendTwilioMessageAction)
        async def send_twilio_message(params: SendTwilioMessageAction):
            """Sends a Twilio message with a customizable message body."""
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
            to_number = os.getenv("TWILIO_WHATSAPP_TO")
            from_number = os.getenv("TWILIO_WHATSAPP_FROM")

            if not all([account_sid, auth_token, to_number, from_number]):
                error_msg = "Missing Twilio credentials in environment variables"
                logger.error(error_msg)
                return ActionResult(error=error_msg)

            message_body = params.message

            curl_command = [
                "curl",
                "-X", "POST",
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                "--data-urlencode", f"To={to_number}",
                "--data-urlencode", f"From={from_number}",
                "--data-urlencode", f"Body={message_body}",
                "-u", f"{account_sid}:{auth_token}"
            ]

            process = await asyncio.create_subprocess_exec(
                *curl_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info(f"WhatsApp message sent: {message_body}")
                return ActionResult(extracted_content="Message sent successfully")
            else:
                error_msg = f"Failed to send WhatsApp message: {stderr.decode()}"
                logger.error(error_msg)
                return ActionResult(error=error_msg)

        @self.registry.action("Verify Twilio Message", param_model=VerifyTwilioMessageAction)
        async def verify_twilio_message(params: VerifyTwilioMessageAction):
            """Receives the second-to-last incoming Twilio message and verifies it."""
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")

            if not all([account_sid, auth_token]):
                error_msg = "Missing Twilio credentials in environment variables"
                logger.error(error_msg)
                return ActionResult(error=error_msg)

            curl_command = [
                "curl",
                "-s",  # Silent mode
                "-X", "GET",
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json?PageSize=2&Direction=inbound",
                "-u", f"{account_sid}:{auth_token}"
            ]

            process = await asyncio.create_subprocess_exec(
                *curl_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                try:
                    response_data = json.loads(stdout)
                    messages = response_data.get('messages', [])

                    if len(messages) >= 2:
                        second_last_message = messages[1]  # Get the second message (index 1)
                        received_message = second_last_message.get('body', '').strip().lower()
                        from_number = second_last_message.get('from', '')

                        log_message = f"Received second-to-last Twilio message from {from_number}: {received_message}"
                        logger.info(log_message)

                        # Verify the received message
                        if params.expected_message:
                            if received_message == params.expected_message.lower():
                                return ActionResult(extracted_content="Message verified successfully.")
                            else:
                                return ActionResult(error="Received message does not match the expected message.")
                        else:
                            # If no expected_message is provided, check for "Yes"
                            if received_message == "yes":
                                return ActionResult(extracted_content="Task completed successfully. User replied 'Yes'.")
                            else:
                                return ActionResult(error=f"User did not reply 'Yes'. Received: {received_message}")
                    else:
                        return ActionResult(error="Not enough messages found to retrieve the second-to-last one.")

                except json.JSONDecodeError:
                    error_message = f"Error decoding Twilio message response: {stdout.decode()}"
                    logger.error(error_message)
                    return ActionResult(error=error_message)
            else:
                error_message = f"Error receiving Twilio message. Error: {stderr.decode()}"
                logger.error(error_message)
                return ActionResult(error=error_message)