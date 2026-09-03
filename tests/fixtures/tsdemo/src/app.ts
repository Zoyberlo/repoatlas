import { User, makeUser, DEFAULT_NAME, Greets } from "./user";

export type Formatter = (value: string) => string;

export class Admin extends User {
  audit(): string {
    return this.greet("admin");
  }
}

export function run(): string {
  const user: Greets = makeUser(DEFAULT_NAME);
  const admin = new Admin("root");
  return user.greet("hi") + admin.audit();
}
