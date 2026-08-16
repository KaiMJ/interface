import { redirect } from "next/navigation";
import { currentTeller } from "@/lib/session";

export default async function Home() {
  redirect((await currentTeller()) ? "/members" : "/login");
}
