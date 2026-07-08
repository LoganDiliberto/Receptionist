import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatListModule } from '@angular/material/list';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    RouterOutlet,
    RouterLink,
    RouterLinkActive,
    MatToolbarModule,
    MatIconModule,
    MatButtonModule,
    MatSidenavModule,
    MatListModule,
  ],
  template: `
    <mat-sidenav-container class="shell">
      <mat-sidenav mode="side" opened class="sidenav">
        <div class="brand">
          <mat-icon>storefront</mat-icon>
          <div>
            <div class="brand-title">Funkle</div>
            <div class="brand-sub">Admin console</div>
          </div>
        </div>
        <mat-nav-list>
          <a mat-list-item routerLink="/" routerLinkActive="active-link"
             [routerLinkActiveOptions]="{ exact: true }">
            <mat-icon matListItemIcon>dashboard</mat-icon>
            <span matListItemTitle>Dashboard</span>
          </a>
          <a mat-list-item routerLink="/staff" routerLinkActive="active-link">
            <mat-icon matListItemIcon>groups</mat-icon>
            <span matListItemTitle>Staff</span>
          </a>
          <a mat-list-item routerLink="/services" routerLinkActive="active-link">
            <mat-icon matListItemIcon>content_cut</mat-icon>
            <span matListItemTitle>Services</span>
          </a>
          <a mat-list-item routerLink="/calendar" routerLinkActive="active-link">
            <mat-icon matListItemIcon>event</mat-icon>
            <span matListItemTitle>Calendar</span>
          </a>
          <a mat-list-item routerLink="/calls" routerLinkActive="active-link">
            <mat-icon matListItemIcon>phone_in_talk</mat-icon>
            <span matListItemTitle>Calls</span>
          </a>
        </mat-nav-list>
      </mat-sidenav>
      <mat-sidenav-content>
        <mat-toolbar class="topbar">
          <span>Receptionist Admin</span>
          <span class="spacer"></span>
        </mat-toolbar>
        <router-outlet />
      </mat-sidenav-content>
    </mat-sidenav-container>
  `,
  styles: [
    `
      .shell {
        height: 100vh;
      }
      .sidenav {
        width: 240px;
        background: #ffffff;
        border-right: 1px solid #e5e7eb;
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 20px 20px 16px;
        border-bottom: 1px solid #e5e7eb;

        mat-icon {
          font-size: 28px;
          width: 28px;
          height: 28px;
          color: #2563eb;
        }
      }
      .brand-title {
        font-size: 18px;
        font-weight: 600;
      }
      .brand-sub {
        font-size: 12px;
        color: #6b7280;
      }
      .topbar {
        background: #ffffff;
        color: rgba(0, 0, 0, 0.85);
        border-bottom: 1px solid #e5e7eb;
        position: sticky;
        top: 0;
        z-index: 2;
      }
      .active-link {
        background: rgba(37, 99, 235, 0.08);
        color: #1d4ed8;
        mat-icon {
          color: #1d4ed8;
        }
      }
      .spacer {
        flex: 1 1 auto;
      }
    `,
  ],
})
export class AppComponent {}
