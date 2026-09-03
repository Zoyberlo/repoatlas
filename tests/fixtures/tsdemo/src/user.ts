export interface Greets {
  greet(name: string): string;
}

export const DEFAULT_NAME = "world";

export class User implements Greets {
  private label: string;

  constructor(label: string) {
    this.label = label;
  }

  greet(name: string): string {
    return `${this.label}: ${name}`;
  }

  shout(): string {
    return this.greet(DEFAULT_NAME).toUpperCase();
  }
}

export function makeUser(label: string): User {
  return new User(label);
}
