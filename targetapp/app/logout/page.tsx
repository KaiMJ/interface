import { redirect } from "next/navigation";
import { signOut } from "@/lib/session";

export default async function Logout() {
  await signOut();
  redirect("/login");
}
