import { api } from '$lib/api/client';
import { pickApiMethods } from '$lib/api/featureApi';

export type {
  AppNotification,
  AppNotificationSummary,
  NotificationPreferences,
  NotificationPreferencesUpdate,
} from '$lib/api/client';

export type NotificationListOptions = Parameters<typeof api.listNotifications>[0];

export const notificationsApi = pickApiMethods([
  'notificationSummary',
  'notificationPreferences',
  'updateNotificationPreferences',
  'listNotifications',
  'markNotificationRead',
  'markAllNotificationsRead',
] as const);

export const {
  notificationSummary,
  notificationPreferences,
  updateNotificationPreferences,
  listNotifications,
  markNotificationRead,
  markAllNotificationsRead,
} = notificationsApi;
