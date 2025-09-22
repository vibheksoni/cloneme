"""
Matrix Platform Implementation for CloneMe

This module implements Matrix support using the matrix-nio client library.
It follows the same pattern as the Discord platform implementation.
"""

import asyncio
from email import message
from platform import platform
from signal import SIGINT, SIGTERM
from typing import Any, List, Optional, TYPE_CHECKING

import re
import datetime

from nio import (
    AsyncClient,
    AsyncClientConfig,
    Event,
    MatrixRoom,
    MatrixUser,
    MegolmEvent,
    MessageDirection,
    ProfileGetAvatarError,
    ProfileGetAvatarResponse,
    RoomMessageText,
    RoomMessagesError,
    RoomMessagesResponse,
    SyncResponse,
    LoginResponse,
    JoinError,
    RoomSendError,
)
from langchain_core.language_models.chat_models import BaseChatModel

from mods.platform.base_platform import BasePlatform, ActiveChat
from mods.platform.platform_manager import PlatformManager
from mods.objects.chats.Chat import Chat
from mods.objects.messages.Message import Message
from mods.objects.person.Person import Person

if TYPE_CHECKING:
    from mods.config import Profile, SettingsManager


class MatrixPlatform(BasePlatform):
    """
    A platform for interacting with Matrix using matrix-nio.
    This class provides Matrix-specific implementation of the BasePlatform interface.
    """

    def __init__(
        self,
        homeserver: str,
        username: str,
        password: str,
        llm: BaseChatModel,
        profile: Optional["Profile"] = None,
        settings_manager: Optional["SettingsManager"] = None,
        device_id: str = "",
        store_path: str = "store",
        **kwargs,
    ):
        """
        Initialize a MatrixPlatform instance.

        Args:
            homeserver (str): The Matrix homeserver URL (e.g., "https://matrix.org")
            username (str): The Matrix username (e.g., "@username:matrix.org")
            password (str): The Matrix password
            llm (BaseChatModel): The language model for AI decision making and response generation
            profile (Optional[Profile]): The profile to use for the bot
            settings_manager (Optional[SettingsManager]): The settings manager for hot-reloadable configuration
            device_id (Optional[str]): Device ID for the Matrix client
            **kwargs: Additional keyword arguments
        """
        super().__init__(llm=llm, profile=profile, settings_manager=settings_manager)

        self.homeserver = homeserver
        self.username = username
        self.password = password

        cfg = AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True)

        # Initialize Matrix client
        # Generate a default device_id if none provided
        if not device_id:
            import uuid

            device_id = f"entangle_{str(uuid.uuid4())[:8]}"

        # Ensure store directory exists
        import os

        os.makedirs(store_path, exist_ok=True)

        self.matrix_client = AsyncClient(
            homeserver=homeserver,
            user=username,
            device_id=device_id,
            store_path=store_path,
            config=cfg,
        )

        try:
            self.matrix_client.user_id = username
        except Exception:
            pass
        self.password = password

        # Track typing contexts
        self._active_typing_contexts = {}

        # Initialize platform manager
        self.platform_manager = PlatformManager(
            llm=llm, profile=profile, settings_manager=settings_manager
        )

        # Setup Matrix event handlers
        self._setup_matrix_events()

    def _setup_matrix_events(self):
        """Set up Matrix client event handlers."""

        self.matrix_client.add_event_callback(self._on_message, RoomMessageText)
        self.matrix_client.add_response_callback(self._on_sync, SyncResponse)

    async def _on_message(self, room: MatrixRoom, event):
        """Handle incoming events"""
        try:
            if isinstance(event, RoomMessageText):
                # Handle text messages
                self.logger.debug(
                    f"Received message in {room.room_id}: {event.body[:50]}..."
                )
                await self._handle_text_messages(room, event)
            elif isinstance(event, MegolmEvent):
                self.logger.debug(
                    f"Received encrypted message in {room.room_id}: {event.parse_decrypted_event}"
                )

        except Exception as e:
            self.logger.error(f"Error in Matrix message handler: {e}")

    async def _on_sync(self, response: SyncResponse):
        """Called on each sync response from the Matrix server."""
        if not self._running:
            self._running = True
            self.logger.info(f"Matrix client synced and ready as {self.username}")

    async def _handle_text_messages(self, room: MatrixRoom, event: RoomMessageText):
        """Handle text messages"""
        try:
            # Skip our own messages
            if event.sender == self.matrix_client.user_id:
                return await self._handle_own_message(event, room)

            # Check if we should always respond based on Matrix-specific settings
            should_always_respond = self._should_always_respond(event, room)
            is_active = self._is_active_chat_message(room)

            self.logger.debug(
                f"Should always respond: {should_always_respond}, Is active: {is_active}"
            )

            # Process if it's an active chat OR if Matrix settings require always responding
            if is_active or should_always_respond:
                return await self._handle_user_message(event, room)
            else:
                self.logger.debug(
                    "Not responding - not active and no always-respond conditions met"
                )

            self.cleanup_inactive_chats()
            return None
        except Exception as e:
            self.logger.error(f"Error on handling text messages: {e}")

    async def _handle_own_message(self, event: RoomMessageText, room: MatrixRoom):
        """Handle messages sent by the bot itself."""
        self.logger.debug(f"Bot sent message in {room.room_id}: {event.body[:50]}...")
        try:
            chat_id = str(room.room_id)
            chat = self.convert_platform_chat(room)
            matrix_user = await self._get_matrix_user(event.sender, room)
            sender = self.convert_platform_user(matrix_user)

            chat.add_participant(sender)
            msg_obj = self.convert_platform_message(event, chat, sender)
            chat.add_message(msg_obj)

            existing_active_chat = next(
                (
                    chat_obj
                    for chat_obj in self.active_chats
                    if chat_obj.active_chat_id == chat_id
                ),
                None,
            )

            if existing_active_chat:
                existing_active_chat.update_last_message_time()
                existing_active_chat.chat_object = chat
            else:
                self.active_chats.append(ActiveChat(chat_id, chat))

            self.cleanup_inactive_chats()

        except Exception as e:
            self.logger.error(f"Error handling own message: {e}")

    async def _handle_user_message(self, event: RoomMessageText, room: MatrixRoom):
        """Handle messages from other users."""
        try:
            # Convert Matrix objects to CloneMe objects
            chat_id = str(room.room_id)
            matrix_user = await self._get_matrix_user(event.sender, room)
            sender = self.convert_platform_user(matrix_user)
            chat = self.convert_platform_chat(room)

            # Build message context
            chat.add_participant(sender)
            msg_obj = self.convert_platform_message(event, chat, sender)
            chat.add_message(msg_obj)

            existing_active_chat = next(
                (
                    chat_obj
                    for chat_obj in self.active_chats
                    if chat_obj.active_chat_id == chat_id
                ),
                None,
            )

            if existing_active_chat:
                existing_active_chat.update_last_message_time()
                existing_active_chat.chat_object = chat

            context_messages = await self.collect_context(room)
            # DM detection
            is_dm = self._is_direct_message(room)
            # Process message through platform manager
            await self.platform_manager.process_message(
                self, msg_obj, context_messages, is_dm_override=is_dm
            )

        except Exception as e:
            self.logger.error(f"Error handling Matrix user message: {e}")

    def _update_person_matrix_info(self, person: Person, matrix_user: MatrixUser):
        """
        Update a Person object with current Matrix user information.

        Args:
            person (Person): The person object to update.
            matrix_user (MatrixUser): The Matrix user object with current info.
        """
        matrix_profile = {
            "user_id": str(matrix_user.user_id),
            "username": matrix_user.name,
            "display_name": matrix_user.display_name,
            "avatar_url": matrix_user.avatar_url,
        }

        if not hasattr(person, "metadata") or person.metadata is None:
            person.metadata = {}
        person.metadata["matrix_profile"] = matrix_profile

        generic_profile = {
            "platform": "matrix",
            "user_id": str(matrix_user.user_id),
            "username": matrix_user.name,
            "display_name": matrix_user.display_name,
            "avatar_url": matrix_user.avatar_url,
            "created_at": matrix_user.last_active_ago,
            "is_bot": False,
        }

        person.metadata["platform_profile"] = generic_profile

        current_identifiers = set(person.get_identifiers())
        new_identifiers = [str(matrix_user.user_id), matrix_user.name]

        if matrix_user.display_name and matrix_user.display_name != matrix_user.name:
            new_identifiers.append(matrix_user.display_name)
        for identifier in new_identifiers:
            if identifier not in current_identifiers:
                person.identifiers.append(identifier)

    async def _get_matrix_user(self, user_id: str, room: MatrixRoom):
        """Get user information from Matrix."""
        # Return a simple user object - in a real implementation,
        # you might want to fetch more user details
        avatar_url = None

        avatar_resp = await self.matrix_client.get_avatar(user_id)

        if isinstance(avatar_resp, ProfileGetAvatarResponse):
            avatar_url = avatar_resp.avatar_url
        elif isinstance(avatar_resp, ProfileGetAvatarError):
            print(f"Error while getting avatar: {avatar_resp.message}")

        return MatrixUser(
            user_id=user_id,
            display_name=room.user_name(user_id) or user_id,
            avatar_url=avatar_url,  # Could be fetched if needed
        )

    def _is_active_chat_message(self, room: MatrixRoom) -> bool:
        """Check if the message is part of an active chat."""
        chat_id = str(room.room_id)
        existing_chat = next(
            (chat for chat in self.active_chats if chat.active_chat_id == chat_id), None
        )
        return existing_chat is not None

    def _is_direct_message(self, room: MatrixRoom) -> bool:
        """Check if a Matrix room is a direct message."""
        return len(room.users) == 2

    def _is_bot_mentioned(self, event: RoomMessageText) -> bool:
        """Check if the bot is mentioned in a Matrix message."""
        if not self.matrix_client.user_id:
            return False

        # Check for actual Matrix User ID mention (user_id already includes @)
        user_id_pattern = rf"{re.escape(self.matrix_client.user_id)}"
        if re.search(user_id_pattern, event.body):
            return True

        if self.profile and self.profile.name:
            name_pattern = rf"{re.escape(self.profile.name)}:"
            if re.search(name_pattern, event.body):
                return True

        return False

    def _should_always_respond(self, event: RoomMessageText, room: MatrixRoom) -> bool:
        """
        Check if the bot should always respond based on Matrix-specific settings.

        Returns:
            bool: True if the bot should always respond, False otherwise
        """
        if not self.settings_manager:
            return False

        is_dm = self._is_direct_message(room)
        self.logger.debug(
            f"Is DM: {is_dm}, Room users: {len(room.users)}, Room name: '{room.display_name}'"
        )

        if is_dm:
            should_respond = self.settings_manager.get(
                "platform_specific.matrix.always_answer_dms", True
            )
            self.logger.debug(f"DM detected - should respond: {should_respond}")
            return should_respond

        is_mentioned = self._is_bot_mentioned(event)
        self.logger.debug(
            f"Is bot mentioned: {is_mentioned}, Bot user_id: {self.matrix_client.user_id}"
        )

        if is_mentioned:
            should_respond = self.settings_manager.get(
                "platform_specific.matrix.always_reply_to_mentions", True
            )
            self.logger.debug(f"Bot mentioned - should respond: {should_respond}")
            return should_respond

        self.logger.debug("No always-respond conditions met")
        return False

    def _extract_mentions(self, event: RoomMessageText) -> List[str]:
        """Extract mentioned user IDs from a Matrix message."""
        mentions = []
        # Matrix mentions format: @username:server.com
        mention_pattern = r"@([^:]+:[^:\s]+)"
        matches = re.findall(mention_pattern, event.body)
        for match in matches:
            mentions.append(f"@{match}")
        return mentions

    def _get_reply_to_message_id(self, event: RoomMessageText) -> Optional[str]:
        """Get the message ID that this Matrix message is replying to."""
        # Matrix replies are in the event content
        if hasattr(event, "source") and event.source.get("content", {}).get(
            "m.relates_to"
        ):
            relation = event.source["content"]["m.relates_to"]
            if relation.get("rel_type") == "m.reply":
                return relation.get("event_id")
        return None

    def _update_active_chat(self, chat_id: str, chat: Chat):
        """Update or add a chat to the active chats list."""
        # Find existing active chat
        for active_chat in self.active_chats:
            if active_chat.active_chat_id == chat_id:
                active_chat.update_last_message_time()
                active_chat.chat_object = chat
                return

        # Create new active chat
        active_chat = ActiveChat(chat_id, chat)
        self.active_chats.append(active_chat)

    # Implementation of abstract methods from BasePlatform

    async def ensure_keys(self) -> None:
        """Upload encryption keys if the client indicates it's necessary"""
        if getattr(self.matrix_client, "should_upload_keys", False):
            await self.matrix_client.keys_upload()

    async def initial_sync(self, timeout_ms: int = 3000) -> None:
        """Perform an initial sync to populate local state.

        Args:
            timeout_ms: Sync timeout in milliseconds.
        """
        await self.matrix_client.sync(timeout=timeout_ms, full_state=True)

    async def sync_forever(self, timeout_ms: int = 30000) -> None:
        """Run the Matrix sync loop indefinitely.

        Args:
            timeout_ms: Sync timeout in milliseconds.
        """
        await self.matrix_client.sync_forever(timeout=30000, full_state=True)

    async def login(self) -> Any:
        """Log in to the Matrix homeserver using the configured credentials."""
        return await self.matrix_client.login(self.password, device_name="entangle")

    def get_platform_name(self) -> str:
        """Get the name of this platform."""
        return "matrix"

    async def send_message(self, chat_id: str, content: str) -> bool:
        """Send a message to a Matrix room."""
        try:
            response = await self.matrix_client.room_send(
                room_id=chat_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": content},
            )

            if isinstance(response, RoomSendError):
                self.logger.error(
                    f"Failed to send message to {chat_id}: {response.message}"
                )
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error sending Matrix message: {e}")
            return False

    async def start_typing(self, chat_id: str) -> None:
        """Start typing indicator in a Matrix room."""
        try:
            await self.matrix_client.room_typing(chat_id, typing_state=True)
            self._active_typing_contexts[chat_id] = True
        except Exception as e:
            self.logger.error(f"Error starting typing in Matrix room {chat_id}: {e}")

    async def stop_typing(self, chat_id: str) -> None:
        """Stop typing indicator in a Matrix room."""
        try:
            await self.matrix_client.room_typing(chat_id, typing_state=False)
            self._active_typing_contexts.pop(chat_id, None)
        except Exception as e:
            self.logger.error(f"Error stopping typing in Matrix room {chat_id}: {e}")

    async def collect_context(
        self, message_ref: Any, max_context: int = 10
    ) -> List[Message]:
        """Collect recent messages from the Matrix room as context."""
        try:
            context: List[Message] = []
            room_id = None
            matrix_room = None

            if isinstance(message_ref, MatrixRoom):
                room_id = message_ref.room_id
                matrix_room = message_ref
            else:
                self.logger.error(
                    f"Unexpected type for message_ref: {type(message_ref)}"
                )

            if not matrix_room or not room_id:
                self.logger.error(f"Could not find Matrix room {room_id}")
                return []

            chat = self.convert_platform_chat(matrix_room)

            history_resp = await self.matrix_client.room_messages(
                room_id=room_id,
                limit=max_context,
                direction=MessageDirection.back,  # backward from most recent
            )

            if isinstance(history_resp, RoomMessagesResponse):
                if hasattr(history_resp, "chunk") and history_resp.chunk:
                    # Get message IDs for flagged message cleanup
                    context_message_ids = [
                        str(event.event_id) for event in history_resp.chunk
                    ]
                    self.remove_flagged_messages_not_in_context(
                        room_id, context_message_ids
                    )

                    flagged_count = 0
                    for event in reversed(history_resp.chunk):
                        try:
                            # Only process message events we can handle
                            if (
                                hasattr(event, "sender")
                                and hasattr(event, "body")
                                and isinstance(event, Event)
                            ):
                                message_id = str(event.event_id)

                                # Check if message is flagged
                                if self.is_message_flagged(message_id):
                                    flagged_count += 1
                                    self.logger.debug(
                                        f"Filtering out flagged message {message_id} from context"
                                    )
                                    continue

                                # Get sender information
                                matrix_user = await self._get_matrix_user(
                                    event.sender, matrix_room
                                )
                                sender = self.convert_platform_user(matrix_user)
                                chat.add_participant(sender)
                                # Convert to Message object
                                msg_obj = self.convert_platform_message(
                                    event, chat, sender
                                )
                                context.append(msg_obj)

                        except Exception as e:
                            self.logger.warning(
                                f"Error processing context message: {e}"
                            )
                            continue

                    if flagged_count > 0:
                        self.logger.debug(
                            f"Context collected: {len(context)} messages ({flagged_count} flagged messages filtered out)"
                        )
            elif isinstance(history_resp, RoomMessagesError):
                self.logger.error(
                    f"Error getting room messages for: {history_resp.room_id}"
                )

            return context
        except Exception as e:
            self.logger.error(f"Error collecting Matrix context: {e}")
            return []

    def convert_platform_message(
        self, platform_msg, chat: Chat, sender: Person
    ) -> Message:
        """Convert a Matrix message to a generic Message object."""
        # Extract mentions and reply info
        mentions = self._extract_mentions(platform_msg)
        reply_to_message_id = self._get_reply_to_message_id(platform_msg)

        timestamp = datetime.datetime.fromtimestamp(
            int(platform_msg.server_timestamp / 1000), tz=datetime.timezone.utc
        )  # sec since 1970
        event_datetime = timestamp.strftime("%Y-%m-%d %H:%M:%S")

        room = self.matrix_client.rooms.get(chat.chat_id)
        sender_nick = None

        if isinstance(room, MatrixRoom):
            sender_nick = room.user_name(platform_msg.sender)
            if not sender_nick:  # convert @foo:mat.io into foo
                sender_nick = platform_msg.sender.split(":")[0][1:]
            room_nick = room.display_name
            if room_nick in (None, "", "Empty Room"):
                room_nick = "Undetermined"

        msg = platform_msg.body
        fixed_msg = re.sub("\n", "\n    ", msg)
        text = fixed_msg
        metadata = {
            "matrix_source": platform_msg.source,
            "matrix_room": room,
            "matrix_room_display_name": room.display_name
            if isinstance(room, MatrixRoom)
            else "",
            "matrix_sender_name": sender_nick,
            "matrix_datetime": event_datetime,
            "matrix_event_id": str(platform_msg.event_id),
            "matrix_room_id": str(room.room_id) if room else chat.chat_id,
        }

        return Message(
            message_id=str(platform_msg.event_id),
            content=text,
            sender=sender,
            created_at=timestamp,
            chat=chat,
            reply_to_message_id=reply_to_message_id,
            mentions=mentions,
            metadata=metadata,
        )

    def convert_platform_user(self, platform_user: MatrixUser) -> Person:
        """Convert a Matrix user to a generic Person object."""
        user_id = str(platform_user.user_id)

        is_bot_self = (
            self.matrix_client.user
            and platform_user.user_id == self.matrix_client.user_id
        )
        person_id = user_id  # Use actual user ID for both bot and regular users
        storage_key = user_id

        if storage_key in self.persons:
            person = self.persons[storage_key]
            self._update_person_matrix_info(person, platform_user)
            return person

        identifiers = [user_id, platform_user.name]

        if (
            platform_user.display_name
            and platform_user.display_name != platform_user.name
        ):
            identifiers.append(platform_user.display_name)

        if is_bot_self and "ai_assistant" not in identifiers:
            identifiers.append("ai_assistant")

        person = Person(person_id=person_id, identifiers=identifiers)
        self._update_person_matrix_info(person, platform_user)
        self.persons[storage_key] = person
        return person

    def convert_platform_chat(self, platform_chat: MatrixRoom) -> Chat:
        """Convert a Matrix room to a generic Chat object."""
        chat = Chat(chat_id=platform_chat.room_id)

        if hasattr(platform_chat, "users"):
            for user in platform_chat.users:
                try:
                    person = self.convert_platform_user(platform_chat.users[user])
                    chat.add_participant(person)
                except Exception as e:
                    print(f"Error converting platform chat: {e}")

        return chat

    async def start_platform(self) -> None:
        """Start the Matrix client and begin listening for events."""
        try:
            self.logger.info("Starting Matrix platform...")

            await self.load_store()
            # Login to Matrix
            login_response = await self.login()
            if isinstance(login_response, LoginResponse):
                self.logger.info(f"Logged in to Matrix as {self.username}")
            else:
                self.logger.error(f"Failed to login to Matrix: {login_response}")
                return
            await self.ensure_keys()
            await self.initial_sync()

            try:
                pass
            except Exception:
                pass

            # Start syncing
            import signal as _signal

            stop = asyncio.Event()
            try:
                loop = asyncio.get_running_loop()
                for sig in (_signal.SIGINT, _signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, stop.set)
                    except Exception:
                        pass
            except Exception:
                pass

            sync_task = asyncio.create_task(self.sync_forever())
            stop_task = asyncio.create_task(stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {sync_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                pass
            except KeyboardInterrupt:
                pass
            finally:
                for t in (sync_task, stop_task):
                    if not t.done():
                        t.cancel()
                try:
                    if hasattr(self, "close"):
                        await self.close()
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(f"Error starting Matrix platform: {e}")
            raise

    def is_running(self) -> bool:
        """Check if the Matrix client is currently running and connected."""
        return self._running and self.matrix_client.logged_in

    def run_platform(self):
        """Run the Matrix platform (blocking call)."""
        try:
            asyncio.run(self.start_platform())
        except KeyboardInterrupt:
            self.logger.info("Matrix platform stopped by user")
        except Exception as e:
            self.logger.error(f"Matrix platform error: {e}")
            raise

    async def join_room(self, room_id: str) -> bool:
        """Join a Matrix room."""
        try:
            response = await self.matrix_client.join(room_id)
            if isinstance(response, JoinError):
                self.logger.error(f"Failed to join room {room_id}: {response.message}")
                return False

            self.logger.info(f"Successfully joined room {room_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error joining Matrix room {room_id}: {e}")
            return False

    async def leave_room(self, room_id: str) -> bool:
        """Leave a Matrix room."""
        try:
            response = await self.matrix_client.room_leave(room_id)
            self.logger.info(f"Left room {room_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error leaving Matrix room {room_id}: {e}")
            return False

    async def load_store(self) -> None:
        """Load local store if available (encryption state)."""
        try:
            result = getattr(self.matrix_client, "load_store", None)
            if callable(result):
                maybe = result()
                if asyncio.iscoroutine(maybe):
                    await maybe
        except Exception as e:
            self.logger.warning(f"Could not load Matrix store: {e}")
            # Continue anyway - the client will create a new store if needed

    async def close(self):
        """Close the Matrix client connection."""
        try:
            await self.matrix_client.logout()
            await self.matrix_client.close()
            self._running = False
            self.logger.info("Matrix client closed")
        except Exception as e:
            self.logger.error(f"Error closing Matrix client: {e}")


__all__ = ["MatrixPlatform"]
