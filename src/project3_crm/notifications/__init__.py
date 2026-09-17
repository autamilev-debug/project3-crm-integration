"""Safe operational notification construction and transport."""

from project3_crm.notifications.service import (
    AlertNotificationAction,
    AlertNotificationOutcome,
    BatchNotificationAction,
    BatchNotificationOutcome,
    NotificationStateError,
    process_cluster_technical_alert,
    process_crm_failure_batch,
    process_crm_pause_alert,
    process_normalization_failure_batch,
)
from project3_crm.notifications.smtp import (
    ClusterTechnicalAlert,
    CrmTerminalFailure,
    NormalizationFailure,
    NotificationMessage,
    NotificationSendCategory,
    NotificationSendResult,
    SmtpNotificationSender,
    build_cluster_technical_alert,
    build_crm_pause_alert,
    build_crm_terminal_failure_batch,
    build_normalization_failure_batch,
)

__all__ = [
    "AlertNotificationAction",
    "AlertNotificationOutcome",
    "BatchNotificationAction",
    "BatchNotificationOutcome",
    "ClusterTechnicalAlert",
    "CrmTerminalFailure",
    "NormalizationFailure",
    "NotificationMessage",
    "NotificationStateError",
    "NotificationSendCategory",
    "NotificationSendResult",
    "SmtpNotificationSender",
    "build_cluster_technical_alert",
    "build_crm_pause_alert",
    "build_crm_terminal_failure_batch",
    "build_normalization_failure_batch",
    "process_crm_failure_batch",
    "process_crm_pause_alert",
    "process_cluster_technical_alert",
    "process_normalization_failure_batch",
]
