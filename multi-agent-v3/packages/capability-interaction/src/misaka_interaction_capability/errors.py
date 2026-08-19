class InteractionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ChannelConflict(InteractionError):
    pass


class ChannelNotFound(InteractionError):
    pass


class ChannelClosed(InteractionError):
    pass


class MessageConflict(InteractionError):
    pass


class MessageNotFound(InteractionError):
    pass


class DeliveryConflict(InteractionError):
    pass
