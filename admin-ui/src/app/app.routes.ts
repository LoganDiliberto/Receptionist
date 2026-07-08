import { Routes } from '@angular/router';

export const APP_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () =>
      import('./pages/dashboard.component').then((m) => m.DashboardComponent),
    title: 'Dashboard',
  },
  {
    path: 'staff',
    loadComponent: () =>
      import('./pages/staff.component').then((m) => m.StaffComponent),
    title: 'Staff',
  },
  {
    path: 'services',
    loadComponent: () =>
      import('./pages/services.component').then((m) => m.ServicesComponent),
    title: 'Services',
  },
  {
    path: 'calendar',
    loadComponent: () =>
      import('./pages/calendar.component').then((m) => m.CalendarComponent),
    title: 'Calendar',
  },
  {
    path: 'calls',
    loadComponent: () =>
      import('./pages/calls.component').then((m) => m.CallsComponent),
    title: 'Calls',
  },
  { path: '**', redirectTo: '' },
];
