import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, of, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';

export interface ScheduleSlot {
  start: string;
  end: string;
}

export interface Staff {
  name: string;
  services: string[];
  schedule: Record<string, ScheduleSlot | null>;
}

export interface Service {
  name: string;
  duration_minutes: number;
  price: number;
}

export interface HoursSlot {
  open: string;
  close: string;
}

export type HoursMap = Record<string, HoursSlot | null>;

export interface Appointment {
  id: string;
  created_at?: string;
  customer_name: string;
  customer_phone: string;
  stylist: string;
  service: string;
  date: string;
  start_time: string;
  end_time?: string;
  // Phase 2 additions — appointments now optionally link to a Client.
  client_id?: number | null;
  client?: ClientSummary | null;
}

export interface AppointmentPayload {
  customer_name: string;
  customer_phone: string;
  stylist: string;
  service: string;
  date: string;
  start_time: string;
  end_time?: string;
}

export interface ClientSummary {
  id: number;
  first_name: string;
  last_name: string;
  phone: string;
  phone_formatted: string;
}

export interface Client extends ClientSummary {
  email: string | null;
  gender: Gender | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export type Gender = 'male' | 'female' | 'nonbinary' | 'unspecified';

export interface ClientPayload {
  first_name: string;
  last_name: string;
  phone: string;
  email?: string | null;
  gender?: Gender | null;
  notes?: string | null;
}

export interface Summary {
  location: string;
  staff_count: number;
  service_count: number;
  appointment_count: number;
  client_count: number;
  call_count: number;
}

export type CallOutcome = 'booked' | 'no_booking';

export interface CallSummary {
  session_id: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  turn_count: number;
  user_turn_count: number;
  outcome: CallOutcome;
  appointment_ids: string[];
}

export interface CallTurn {
  at: string;
  role: 'user' | 'assistant';
  text: string;
}

export interface CallDetail extends CallSummary {
  turns: CallTurn[];
  appointments: Appointment[];
}

/**
 * Build the API base URL.
 *
 * In production the app is served from the same FastAPI process at
 * `/admin/`, so we can use relative `/api/...` URLs. In `ng serve` the app
 * lives on :4200 and needs an absolute base — the server enables CORS for
 * that origin.
 */
function apiBase(): string {
  if (typeof window !== 'undefined' && window.location.port === '4200') {
    return 'http://127.0.0.1:7860/api';
  }
  return '/api';
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly base = apiBase();

  // --- Meta ---
  summary(): Observable<Summary> {
    return this.http.get<Summary>(`${this.base}/summary`).pipe(catchError(handle));
  }

  // --- Staff ---
  listStaff(): Observable<Staff[]> {
    return this.http.get<Staff[]>(`${this.base}/staff`).pipe(catchError(handle));
  }
  createStaff(staff: Staff): Observable<Staff> {
    return this.http.post<Staff>(`${this.base}/staff`, staff).pipe(catchError(handle));
  }
  updateStaff(originalName: string, staff: Staff): Observable<Staff> {
    return this.http
      .put<Staff>(`${this.base}/staff/${encodeURIComponent(originalName)}`, staff)
      .pipe(catchError(handle));
  }
  deleteStaff(name: string): Observable<unknown> {
    return this.http
      .delete(`${this.base}/staff/${encodeURIComponent(name)}`)
      .pipe(catchError(handle));
  }

  // --- Services ---
  listServices(): Observable<Service[]> {
    return this.http.get<Service[]>(`${this.base}/services`).pipe(catchError(handle));
  }
  createService(service: Service): Observable<Service> {
    return this.http.post<Service>(`${this.base}/services`, service).pipe(catchError(handle));
  }
  updateService(originalName: string, service: Service): Observable<Service> {
    return this.http
      .put<Service>(`${this.base}/services/${encodeURIComponent(originalName)}`, service)
      .pipe(catchError(handle));
  }
  deleteService(name: string): Observable<unknown> {
    return this.http
      .delete(`${this.base}/services/${encodeURIComponent(name)}`)
      .pipe(catchError(handle));
  }

  // --- Hours / Location ---
  getHours(): Observable<HoursMap> {
    return this.http.get<HoursMap>(`${this.base}/hours`).pipe(catchError(handle));
  }
  updateHours(hours: HoursMap): Observable<HoursMap> {
    return this.http
      .put<HoursMap>(`${this.base}/hours`, { hours })
      .pipe(catchError(handle));
  }
  getLocation(): Observable<{ location: string }> {
    return this.http
      .get<{ location: string }>(`${this.base}/location`)
      .pipe(catchError(handle));
  }
  updateLocation(location: string): Observable<{ location: string }> {
    return this.http
      .put<{ location: string }>(`${this.base}/location`, { location })
      .pipe(catchError(handle));
  }

  // --- Calls ---
  listCalls(): Observable<CallSummary[]> {
    return this.http.get<CallSummary[]>(`${this.base}/calls`).pipe(catchError(handle));
  }
  getCall(sessionId: string): Observable<CallDetail> {
    return this.http
      .get<CallDetail>(`${this.base}/calls/${encodeURIComponent(sessionId)}`)
      .pipe(catchError(handle));
  }

  // --- Appointments ---
  listAppointments(start?: string, end?: string): Observable<Appointment[]> {
    let params = new HttpParams();
    if (start) params = params.set('start', start);
    if (end) params = params.set('end', end);
    return this.http
      .get<Appointment[]>(`${this.base}/appointments`, { params })
      .pipe(catchError(handle));
  }
  createAppointment(a: AppointmentPayload): Observable<Appointment> {
    return this.http
      .post<Appointment>(`${this.base}/appointments`, a)
      .pipe(catchError(handle));
  }
  updateAppointment(id: string, a: AppointmentPayload): Observable<Appointment> {
    return this.http
      .put<Appointment>(`${this.base}/appointments/${encodeURIComponent(id)}`, a)
      .pipe(catchError(handle));
  }
  deleteAppointment(id: string): Observable<unknown> {
    return this.http
      .delete(`${this.base}/appointments/${encodeURIComponent(id)}`)
      .pipe(catchError(handle));
  }

  // --- Clients ---
  listClients(query?: string): Observable<Client[]> {
    let params = new HttpParams();
    if (query) params = params.set('q', query);
    return this.http
      .get<Client[]>(`${this.base}/clients`, { params })
      .pipe(catchError(handle));
  }
  getClient(id: number): Observable<Client> {
    return this.http
      .get<Client>(`${this.base}/clients/${id}`)
      .pipe(catchError(handle));
  }
  getClientAppointments(id: number, upcomingOnly = false): Observable<Appointment[]> {
    let params = new HttpParams();
    if (upcomingOnly) params = params.set('upcoming_only', 'true');
    return this.http
      .get<Appointment[]>(`${this.base}/clients/${id}/appointments`, { params })
      .pipe(catchError(handle));
  }
  getClientByPhone(phone: string): Observable<Client | null> {
    // 404 collapses to null so callers can render "new client" affordances
    // without an error surface. Any other error still propagates.
    return this.http
      .get<Client>(`${this.base}/clients/by-phone/${encodeURIComponent(phone)}`)
      .pipe(
        catchError((err: HttpErrorResponse) => {
          if (err.status === 404) return of(null);
          return handle(err);
        }),
      );
  }
  createClient(payload: ClientPayload): Observable<Client> {
    return this.http
      .post<Client>(`${this.base}/clients`, payload)
      .pipe(catchError(handle));
  }
  updateClient(id: number, payload: ClientPayload): Observable<Client> {
    return this.http
      .put<Client>(`${this.base}/clients/${id}`, payload)
      .pipe(catchError(handle));
  }
  deleteClient(id: number): Observable<unknown> {
    return this.http
      .delete(`${this.base}/clients/${id}`)
      .pipe(catchError(handle));
  }
}

function handle(err: HttpErrorResponse): Observable<never> {
  const detail = (err.error as { detail?: string } | null)?.detail;
  const message = detail || err.message || 'Request failed.';
  return throwError(() => new Error(message));
}

export const WEEKDAYS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const;
export type Weekday = (typeof WEEKDAYS)[number];
