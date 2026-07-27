import { CommonModule } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';

import { ApiService, Summary } from '../api.service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
  ],
  template: `
    <div class="page">
      <div class="page-header">
        <h1>Overview</h1>
      </div>

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else if (error()) {
        <div class="error-banner">{{ error() }}</div>
      } @else if (summary(); as s) {
        <div class="tiles">
          <mat-card class="tile" routerLink="/staff">
            <mat-card-content>
              <div class="tile-header">
                <mat-icon>groups</mat-icon>
                <span class="tile-label">Staff</span>
              </div>
              <div class="tile-value">{{ s.staff_count }}</div>
              <div class="tile-sub">On the team</div>
            </mat-card-content>
          </mat-card>

          <mat-card class="tile" routerLink="/services">
            <mat-card-content>
              <div class="tile-header">
                <mat-icon>content_cut</mat-icon>
                <span class="tile-label">Services</span>
              </div>
              <div class="tile-value">{{ s.service_count }}</div>
              <div class="tile-sub">Offered</div>
            </mat-card-content>
          </mat-card>

          <mat-card class="tile" routerLink="/calendar">
            <mat-card-content>
              <div class="tile-header">
                <mat-icon>event</mat-icon>
                <span class="tile-label">Appointments</span>
              </div>
              <div class="tile-value">{{ s.appointment_count }}</div>
              <div class="tile-sub">On the calendar</div>
            </mat-card-content>
          </mat-card>

          <mat-card class="tile" routerLink="/clients">
            <mat-card-content>
              <div class="tile-header">
                <mat-icon>contacts</mat-icon>
                <span class="tile-label">Clients</span>
              </div>
              <div class="tile-value">{{ s.client_count }}</div>
              <div class="tile-sub">On file</div>
            </mat-card-content>
          </mat-card>

          <mat-card class="tile" routerLink="/calls">
            <mat-card-content>
              <div class="tile-header">
                <mat-icon>phone_in_talk</mat-icon>
                <span class="tile-label">Calls</span>
              </div>
              <div class="tile-value">{{ s.call_count }}</div>
              <div class="tile-sub">Handled by the bot</div>
            </mat-card-content>
          </mat-card>
        </div>

        <mat-card class="location-card">
          <mat-card-header>
            <mat-card-title>Location</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <p class="location-text">{{ s.location || 'No location set.' }}</p>
          </mat-card-content>
        </mat-card>
      }
    </div>
  `,
  styles: [
    `
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
        margin-bottom: 24px;
      }
      .tile {
        cursor: pointer;
        transition: transform 0.1s ease, box-shadow 0.1s ease;
      }
      .tile:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
      }
      .tile-header {
        display: flex;
        align-items: center;
        gap: 8px;
        color: #6b7280;
        font-size: 14px;
        margin-bottom: 8px;

        mat-icon {
          font-size: 20px;
          width: 20px;
          height: 20px;
        }
      }
      .tile-value {
        font-size: 40px;
        font-weight: 500;
        line-height: 1;
      }
      .tile-sub {
        color: #6b7280;
        font-size: 13px;
        margin-top: 6px;
      }
      .location-card {
        max-width: 480px;
      }
      .location-text {
        margin: 0;
        color: rgba(0, 0, 0, 0.75);
      }
    `,
  ],
})
export class DashboardComponent implements OnInit {
  private readonly api = inject(ApiService);

  readonly summary = signal<Summary | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.api.summary().subscribe({
      next: (s) => {
        this.summary.set(s);
        this.loading.set(false);
      },
      error: (e: Error) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
  }
}
